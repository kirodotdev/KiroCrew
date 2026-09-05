/**
 * The identity half of the builtin registry, and the property the
 * `origin: 'builtin'` literal in `BuiltinAppRoute` rests on.
 *
 * Three things are asserted here, none of them visible to a type checker:
 *
 * 1. **appId agrees with the app.json on disk.** The appId is a storage key and a
 *    query-key prefix, so a wrong one is not cosmetic — it is a namespace nothing
 *    else on the platform addresses, holding state no other reader can find. The
 *    `/worlds` → `agent-worlds` case is called out separately because it is the
 *    reason appId is explicit data instead of `route.slice(1)`.
 * 2. **The registration seam refuses an id it cannot use as a key.**
 * 3. **The registry has no data-ingestion path.** This is what makes the literal
 *    safe: membership in the registry means the component is compiled into the
 *    host bundle, which an external app cannot achieve. If someone later wires
 *    the registry to accept externally-supplied entries, the literal silently
 *    becomes a lie — so that possibility fails here instead.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { lazy } from 'react'
import { render, screen } from '@testing-library/react'
import { readdirSync, readFileSync, existsSync } from 'node:fs'
import { join, resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  BUILTIN_COMPONENT_REGISTRY,
  getBuiltinApp,
  hasBuiltinComponent,
  registerBuiltinComponents,
  type BuiltinAppEntry,
} from '../apps/builtinRegistry'
import { isValidAppId } from '../apps/appIdentity'
import { AppIdentityProvider, useAppIdentity, useTrustedAppId } from '../app-sdk/identity'

const HERE = dirname(fileURLToPath(import.meta.url))

/** Every shipped builtin manifest, read the way `appManifest.test.ts` reads them. */
function shippedManifests(): { name: string; routes: string[] }[] {
  const dir = resolve(HERE, '../../../src/kiro_crew/apps/builtins')
  return readdirSync(dir, { withFileTypes: true })
    .filter((e) => e.isDirectory() && !e.name.startsWith('.') && !e.name.startsWith('_'))
    .map((e) => join(dir, e.name, 'app.json'))
    .filter((f) => existsSync(f))
    .map((f) => JSON.parse(readFileSync(f, 'utf8')))
    .map((m) => ({
      name: m.name as string,
      routes: ((m.ui?.pages ?? []) as { route?: string }[])
        .map((p) => p.route)
        .filter((r): r is string => typeof r === 'string'),
    }))
}

const dummy = () => lazy(async () => ({ default: () => null }))

describe('builtin registry appIds', () => {
  const manifests = shippedManifests()
  const coreRoutes = Object.keys(BUILTIN_COMPONENT_REGISTRY)

  it('reads a real manifest set, so the assertions below cannot pass vacuously', () => {
    expect(manifests.length).toBeGreaterThan(10)
    expect(coreRoutes.length).toBeGreaterThan(15)
  })

  it('holds every appId to the storage-key charset', () => {
    for (const route of coreRoutes) {
      const { appId } = BUILTIN_COMPONENT_REGISTRY[route]
      expect(isValidAppId(appId), `${route} declares an unusable appId ${JSON.stringify(appId)}`).toBe(true)
    }
  })

  it('names an app that ships an app.json, which also declares that route', () => {
    const byName = new Map(manifests.map((m) => [m.name, m]))
    for (const route of coreRoutes) {
      const { appId } = BUILTIN_COMPONENT_REGISTRY[route]
      const manifest = byName.get(appId)
      expect(manifest, `${route} claims appId '${appId}', which ships no app.json`).toBeDefined()
      expect(
        manifest!.routes,
        `app '${appId}' does not declare route ${route} in ui.pages`,
      ).toContain(route)
    }
  })

  it('registers a page for every route a manifest declares', () => {
    // The other direction. A manifest route with no registry entry puts a nav
    // item in the rail that redirects to /chat when clicked — the silent-vanish
    // failure, and invisible until someone tries the link.
    for (const m of manifests) {
      for (const route of m.routes) {
        expect(hasBuiltinComponent(route), `app '${m.name}' declares ${route} with no registered page`).toBe(true)
      }
    }
  })

  it('takes /worlds from the manifest, not from the route', () => {
    // The single strongest reason appId is explicit data: `route.slice(1)` here
    // would mint 'worlds', which is not an app on this platform. Since the appId
    // becomes a storage-key and query-key prefix, that namespace would be
    // permanent and agreed with by nothing else.
    expect(getBuiltinApp('/worlds')?.appId).toBe('agent-worlds')
    expect(getBuiltinApp('/worlds')?.appId).not.toBe('worlds')
    expect(manifests.some((m) => m.name === 'worlds')).toBe(false)
  })
})

describe('registerBuiltinComponents — appId refusals', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it.each([
    ['', 'empty'],
    ['.', 'current segment'],
    ['..', 'traversal'],
    ['../aws-control', 'traversal into another app'],
    ['Zzq_Id', 'outside the charset'],
    ['zzq id', 'whitespace'],
    ['zzq/nested', 'separator'],
  ])('refuses appId %j (%s) and does not register the route', (appId) => {
    const route = '/zzq-identity-refused'
    expect(() =>
      registerBuiltinComponents({ [route]: { component: dummy(), appId } as BuiltinAppEntry }),
    ).toThrow(/not a valid app id/)
    expect(hasBuiltinComponent(route)).toBe(false)
  })

  it('refuses an entry with no appId at all', () => {
    const route = '/zzq-identity-missing'
    expect(() =>
      registerBuiltinComponents({ [route]: { component: dummy() } as unknown as BuiltinAppEntry }),
    ).toThrow(/not a valid app id/)
    expect(hasBuiltinComponent(route)).toBe(false)
  })

  it('accepts a well-formed entry and hands back the appId', () => {
    const component = dummy()
    registerBuiltinComponents({ '/zzq-identity-ok': { component, appId: 'zzq-identity-ok' } })
    expect(getBuiltinApp('/zzq-identity-ok')).toEqual({ component, appId: 'zzq-identity-ok' })
  })
})

describe('the builtin gate', () => {
  function Probe() {
    const identity = useAppIdentity()
    const trusted = useTrustedAppId()
    return (
      <div>
        <span data-testid="identity">{identity ? `${identity.appId}:${identity.origin}` : 'none'}</span>
        <span data-testid="trusted">{trusted ?? 'refused'}</span>
      </div>
    )
  }

  it('grants the namespace to a builtin origin', () => {
    render(
      <AppIdentityProvider appId="aws-control" origin="builtin">
        <Probe />
      </AppIdentityProvider>,
    )
    expect(screen.getByTestId('identity').textContent).toBe('aws-control:builtin')
    expect(screen.getByTestId('trusted').textContent).toBe('aws-control')
  })

  it('refuses the namespace to a non-builtin origin, keeping identity readable', () => {
    // The external path — AppHost passes the installed app's own origin. An
    // external app CAN self-register under a builtin's name, so if the namespace
    // followed the name alone it would land in that builtin's keys. Identity is
    // still published, because `useAppInfo()` depends on it; the namespace is not.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    render(
      <AppIdentityProvider appId="aws-control-lookalike" origin="external">
        <Probe />
      </AppIdentityProvider>,
    )
    expect(screen.getByTestId('identity').textContent).toBe('aws-control-lookalike:external')
    expect(screen.getByTestId('trusted').textContent).toBe('refused')
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('builtin-only'))
    warn.mockRestore()
  })

  it('reports no identity outside an app page', () => {
    render(<Probe />)
    expect(screen.getByTestId('identity').textContent).toBe('none')
    expect(screen.getByTestId('trusted').textContent).toBe('refused')
  })

  it('publishes identity on the consumer\u2019s FIRST render, not in an effect', () => {
    // The ordering the design calls load-bearing, asserted where Suspense cannot
    // mask it. Recording per render rather than reading the settled DOM: an
    // effect-published identity ends up on screen too, one render later, so the
    // final state passes either way. Only the first entry distinguishes them.
    //
    // The consumer here renders SYNCHRONOUSLY, on purpose. Under a `React.lazy`
    // child the distinction disappears — the parent commits and its effects run
    // while the child is still suspended — so a lazy-only test would pass with
    // the publication in an effect. That masking is not something to rely on: a
    // second visit to the same app in one session finds the module already
    // loaded, React renders the page in the SAME pass, and an effect would then
    // be a render too late. A repeat visit is the case this whole feature exists
    // to serve.
    const perRender: (AppIdentity | null)[] = []
    function RecordingProbe() {
      perRender.push(useAppIdentity())
      return null
    }
    render(
      <AppIdentityProvider appId="aws-control" origin="builtin">
        <RecordingProbe />
      </AppIdentityProvider>,
    )
    expect(perRender.length).toBeGreaterThan(0)
    expect(perRender[0]).toEqual({ appId: 'aws-control', origin: 'builtin' })
  })

  it('keeps one context object across an ancestor repaint', () => {
    // A consumer may key a query or an effect off this value, so a fresh object
    // per parent render would re-run that work on every unrelated repaint.
    const identities: (AppIdentity | null)[] = []
    function Recorder() {
      identities.push(useAppIdentity())
      return null
    }
    function Host({ tick }: { tick: number }) {
      return (
        <AppIdentityProvider appId="aws-control" origin="builtin">
          <span data-testid="tick">{tick}</span>
          <Recorder />
        </AppIdentityProvider>
      )
    }
    const { rerender } = render(<Host tick={1} />)
    rerender(<Host tick={2} />)
    expect(screen.getByTestId('tick').textContent).toBe('2')
    expect(identities.length).toBeGreaterThan(1)
    expect(identities[1]).toBe(identities[0])
  })
})

describe('the registry cannot be filled from data', () => {
  /**
   * `BuiltinAppRoute` publishes `origin: 'builtin'` as a literal, and the proof is
   * that registry membership requires module code in this bundle. That proof holds
   * only while the registry has no ingestion path, which a type checker cannot see —
   * so assert it against the file on disk. Add a fetch, a `/api/` read, or a
   * `window`/storage read to the registry and this fails, which is the point: at
   * that moment the literal would need to become a real origin check.
   */
  const source = readFileSync(resolve(HERE, '../apps/builtinRegistry.ts'), 'utf8')
  /**
   * Comments removed first. A regex over raw source also matches its own prose —
   * the entry docstring names `/api/apps` to say where an appId comes from — so
   * scanning the file as written would fail on documentation and, worse, would
   * pass on ingestion code that someone had commented a description around.
   * (Line-comment stripping is naive about `//` inside a string literal; this
   * module has none, and one would show up as a failure here rather than a pass.)
   */
  const code = source.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1')

  it('reads the real module source, so this cannot pass vacuously', () => {
    expect(code).toContain('BUILTIN_COMPONENT_REGISTRY')
    expect(code.length).toBeGreaterThan(500)
    // The stripper left the code behind, not just the comments.
    expect(code).toContain('export function registerBuiltinComponents')
  })

  it.each([
    [/\bfetch\s*\(/, 'a network read'],
    [/['"`]\/api\//, 'a gateway path'],
    [/\bwindow\b/, 'a global read'],
    [/\b(?:localStorage|sessionStorage)\b/, 'browser storage'],
    [/useQuery|queryClient|getQueryData/, 'the query cache'],
  ])('has no %s (%s)', (pattern) => {
    expect(code).not.toMatch(pattern)
  })

  it('imports only module-local code', () => {
    const specifiers = [...code.matchAll(/^\s*import\s[^\n]*?from\s+'([^']+)'/gm)].map((m) => m[1])
    expect(specifiers.length).toBeGreaterThan(0)
    for (const spec of specifiers) {
      expect(spec === 'react' || spec.startsWith('./'), `unexpected registry import: ${spec}`).toBe(true)
    }
  })
})
