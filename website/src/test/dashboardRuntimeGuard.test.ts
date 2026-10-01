/**
 * Regression tests for the dashboard-runtime source guard
 * (`scripts/check-dashboard-runtime.mjs`).
 *
 * test-audit authoring gate:
 *  1. Observable behavior: the detector flags a root-literal `fetch('/…')` and a
 *     raw `new WebSocket(`, and the zero-tolerance gate fails any file carrying
 *     one (outside the runtime module) — while migrated forms and external URLs
 *     are not flagged.
 *  2. Credible regression: if the detector stopped matching a bypass, or the
 *     gate stopped failing a file that carries one, a new gateway URL could
 *     escape the relay prefix undetected.
 *  3. Existing coverage gap: nothing else enforces "gateway URLs go through the
 *     runtime seam"; this is that architecture contract's only guard.
 *  4. Production seam: the detector/gate are pure functions exported from the
 *     guard script itself — no test-only production seam is introduced.
 */
import { describe, it, expect } from 'vitest'
import { scanSource, computeViolations } from '../../scripts/check-dashboard-runtime.mjs'
import { parseSource } from '../../scripts/gatewayFetchClassifier.mjs'

describe('scanSource — bypass detection', () => {
  it('flags a new root-literal fetch (all quote styles, incl. window.fetch)', () => {
    expect(scanSource(`fetch('/api/status')`).fetchLiterals).toBe(1)
    expect(scanSource('fetch(`/api/ws`)').fetchLiterals).toBe(1)
    expect(scanSource(`fetch("/api/x")`).fetchLiterals).toBe(1)
    expect(scanSource(`window.fetch('/api/x')`).fetchLiterals).toBe(1)
    expect(scanSource(`fetch('/a'); fetch('/b')`).fetchLiterals).toBe(2)
  })

  it('does not flag migrated forms or external URLs', () => {
    // The runtime seam and the loose resolver client.ts adopts:
    expect(scanSource(`dashboardFetch(gatewayPath('/api/x'))`).fetchLiterals).toBe(0)
    expect(scanSource(`fetch(relocateRequestUrl(url), { method: 'POST' })`).fetchLiterals).toBe(0)
    // External and non-same-dashboard targets:
    expect(scanSource(`fetch('https://fonts.googleapis.com/x')`).fetchLiterals).toBe(0)
    expect(scanSource(`fetch('//cdn.example/x')`).fetchLiterals).toBe(0) // protocol-relative
    expect(scanSource(`fetch(url)`).fetchLiterals).toBe(0) // bare param — relocated at its own boundary
  })

  it('flags the template/const/concat bypasses a regex missed (AST classifier)', () => {
    // `const API = '/api/apps/x'` then a template, a bare identifier, or a concat.
    expect(scanSource("const API = '/api/apps/x'\nfetch(`${API}/status`)").fetchLiterals).toBe(1)
    expect(scanSource("const API = '/api/upload/file'\nfetch(API)").fetchLiterals).toBe(1)
    expect(scanSource("const BASE = '/api/x'\nfetch(BASE + path)").fetchLiterals).toBe(1)
    expect(scanSource("const API = '/api/x'\nnew EventSource(`${API}/stream`)").eventSourceLiterals).toBe(1)
    // A non-root const (external base) is NOT a same-dashboard bypass.
    expect(scanSource("const CDN = 'https://cdn/x'\nfetch(`${CDN}/a`)").fetchLiterals).toBe(0)
    // Already relocated is fine even with a root const.
    expect(
      scanSource("const API = '/api/x'\nfetch(relocateRequestUrl(`${API}/a`))").fetchLiterals,
    ).toBe(0)
  })

  it('flags a raw WebSocket constructor but not dashboardWebSocket', () => {
    expect(scanSource(`const ws = new WebSocket(url)`).rawWebSockets).toBe(1)
    expect(scanSource(`new  WebSocket('x')`).rawWebSockets).toBe(1)
    expect(scanSource(`dashboardWebSocket('/api/ws')`).rawWebSockets).toBe(0)
    expect(scanSource(`if (ws.readyState === WebSocket.OPEN) {}`).rawWebSockets).toBe(0)
  })

  it('flags a root-literal EventSource but not a relocated one', () => {
    expect(scanSource(`new EventSource('/api/file-watch')`).eventSourceLiterals).toBe(1)
    expect(scanSource('new EventSource(`/api/apps/x/${id}/stream`)').eventSourceLiterals).toBe(1)
    expect(scanSource(`new EventSource(relocateRequestUrl('/api/x'))`).eventSourceLiterals).toBe(0)
    expect(scanSource(`new EventSource('https://ext/x')`).eventSourceLiterals).toBe(0)
    expect(scanSource(`new EventSource(url)`).eventSourceLiterals).toBe(0)
  })
})

describe('scanSource — imported / derived same-dashboard constants (blind-spot close)', () => {
  it('flags a template-DERIVED same-file root const used bare or in a template', () => {
    // `const R = `${API}/y`` is root-absolute because API is — the shape every
    // route in crew-companion/constants.ts uses. Before the fixpoint upgrade this
    // was invisible (R was not a plain string literal).
    expect(scanSource("const API='/api/x'\nconst R=`${API}/y`\nfetch(R)").fetchLiterals).toBe(1)
    expect(
      scanSource("const API='/api/x'\nconst R=`${API}/y`\nfetch(`${R}?q=1`)").fetchLiterals,
    ).toBe(1)
  })

  it('flags a fetch of an IMPORTED same-dashboard path constant (was a silent bypass)', () => {
    const constantsSrc =
      "export const API_BASE = '/api/apps/x'\nexport const CONFIG_PATH = `${API_BASE}/config`\n"
    const loadModule = (_fromRel: string, importPath: string) =>
      importPath === './constants'
        ? { rel: 'src/apps/x/constants.ts', sf: parseSource('constants.ts', constantsSrc) }
        : null
    const src = "import { CONFIG_PATH } from './constants'\nfetch(CONFIG_PATH)"
    // With the cross-file resolver the imported root const is classified.
    expect(scanSource(src, 'src/apps/x/petBridge.ts', loadModule).fetchLiterals).toBe(1)
    // Same call routed through the seam is fine.
    expect(
      scanSource(
        "import { CONFIG_PATH } from './constants'\nfetch(relocateRequestUrl(CONFIG_PATH))",
        'src/apps/x/petBridge.ts',
        loadModule,
      ).fetchLiterals,
    ).toBe(0)
    // Without the resolver (same-file only) the imported const is NOT visible —
    // the exact blind spot this guard change closes. Proven, so a regression that
    // dropped the resolver would fail here rather than silently reopen it.
    expect(scanSource(src, 'src/apps/x/petBridge.ts').fetchLiterals).toBe(0)
  })

  it('does not flag an imported NON-root (external) constant', () => {
    const modSrc = "export const CDN = 'https://cdn/x'\n"
    const loadModule = () => ({ rel: 'c.ts', sf: parseSource('c.ts', modSrc) })
    expect(
      scanSource("import { CDN } from './c'\nfetch(`${CDN}/a`)", 'src/x.ts', loadModule)
        .fetchLiterals,
    ).toBe(0)
  })
})

describe('scanSource — function-local derived roots and gateway helper params', () => {
  // The confirmed migration gap: the old guard proved root literals and
  // module-scope consts but pinned a function-LOCAL derived root path — and a
  // root-defaulted helper parameter — as safe. These are the exact shapes the
  // shipped wrappers used (aws-control `const url = ${BASE}${path}`, dev-fleet
  // `const url = base + path` with `base = BASE`, PublishHub
  // `const endpoint = provider || '/api/deploy/deploy'`).

  it('flags a function-local const derived from a module root base', () => {
    expect(
      scanSource("const BASE='/api/apps/x'\nfunction f(p){ const url = BASE + p; return fetch(url) }")
        .fetchLiterals,
    ).toBe(1)
    // Template form of the same local derivation (aws-control's shape).
    expect(
      scanSource("const BASE='/api/apps/x'\nfunction f(p){ const url = `${BASE}${p}`; return fetch(url) }")
        .fetchLiterals,
    ).toBe(1)
  })

  it('flags a local derived from a parameter whose DEFAULT is a root const', () => {
    // dev-fleet: `async function request(path, opts, base = BASE) { const url = base + path; fetch(url) }`
    expect(
      scanSource(
        "const BASE='/apps/dev-fleet/api'\nfunction request(path, base = BASE){ const url = base + path; return fetch(url) }",
      ).fetchLiterals,
    ).toBe(1)
  })

  it('flags an `||` / `??` fallback to a root literal', () => {
    // PublishHub: `const endpoint = selected.app?.endpoint || '/api/deploy/deploy'`
    expect(
      scanSource("function f(ep){ const endpoint = ep || '/api/deploy/deploy'; return fetch(endpoint) }")
        .fetchLiterals,
    ).toBe(1)
    expect(
      scanSource("function f(ep){ const endpoint = ep ?? '/api/deploy/deploy'; return fetch(endpoint) }")
        .fetchLiterals,
    ).toBe(1)
  })

  it('flags a local-derived root reaching new EventSource', () => {
    expect(
      scanSource("const API='/api/x'\nfunction f(id){ const u = `${API}/${id}/stream`; return new EventSource(u) }")
        .eventSourceLiterals,
    ).toBe(1)
  })

  it('does NOT flag a bare, unresolvable gateway helper parameter (relocated at its own boundary)', () => {
    // A helper whose param cannot be proven root (e.g. app-sdk's `fetch(safePath)`
    // where `safePath = check(path)`) is NOT pinned as a root escape by name — it
    // is the helper's own job to relocate at the boundary. The scope-aware
    // resolver must not mis-flag it, or every external `fetch(param)` would fail.
    expect(scanSource("function f(safePath){ return fetch(safePath) }").fetchLiterals).toBe(0)
    expect(scanSource("function f(){ const p = check(x); return fetch(p) }").fetchLiterals).toBe(0)
    // Relocating that same helper param at its boundary is the accepted form.
    expect(
      scanSource("function f(safePath){ return fetch(relocateRequestUrl(safePath)) }").fetchLiterals,
    ).toBe(0)
  })

  it('does NOT leak a root local into a sibling scope with the same name', () => {
    // Scope-awareness: one function's `const url = '/api/x'` must not make a
    // DIFFERENT function's external `fetch(url)` look like a root escape.
    const src =
      "function a(){ const url = '/api/x'; return fetch(relocateRequestUrl(url)) }\n" +
      "function b(extUrl){ const url = extUrl; return fetch(url) }"
    expect(scanSource(src).fetchLiterals).toBe(0)
  })

  it('does NOT flag a local derived from an external base', () => {
    expect(
      scanSource("function f(p){ const u = 'https://cdn/x' + p; return fetch(u) }").fetchLiterals,
    ).toBe(0)
  })
})

describe('computeViolations — the zero-tolerance gate', () => {
  it('passes when every scanned file carries no literal fetch and no raw socket', () => {
    const counts = {
      'src/api/client/agents.ts': { fetchLiterals: 0, rawWebSockets: 0 },
      'src/hooks/useWebSocket.ts': { fetchLiterals: 0, rawWebSockets: 0 },
    }
    expect(computeViolations(counts)).toEqual([])
  })

  it('fails any root-literal fetch (no baseline — zero tolerance)', () => {
    const counts = { 'src/api/client/agents.ts': { fetchLiterals: 1, rawWebSockets: 0 } }
    const violations = computeViolations(counts)
    expect(violations).toHaveLength(1)
    expect(violations[0]).toContain('same-dashboard fetch bypass')
  })

  it('fails a bypass in any file', () => {
    const counts = { 'src/pages/NewThing.tsx': { fetchLiterals: 1, rawWebSockets: 0 } }
    expect(computeViolations(counts)).toHaveLength(1)
  })

  it('fails a raw WebSocket anywhere except the runtime module', () => {
    expect(
      computeViolations({ 'src/apps/x/foo.ts': { fetchLiterals: 0, rawWebSockets: 1 } }),
    ).toHaveLength(1)
    // The runtime module owns the one blessed constructor and carries no fetch.
    expect(
      computeViolations({ 'src/lib/dashboardRuntime.ts': { fetchLiterals: 0, rawWebSockets: 2 } }),
    ).toEqual([])
  })

  it('fails a root-literal EventSource stream', () => {
    expect(
      computeViolations({ 'src/hooks/useFileWatch.ts': { fetchLiterals: 0, rawWebSockets: 0, eventSourceLiterals: 1 } }),
    ).toHaveLength(1)
  })

  it('does not flag the runtime module for its own blessed constructor', () => {
    expect(
      computeViolations({ 'src/lib/dashboardRuntime.ts': { fetchLiterals: 0, rawWebSockets: 1 } }),
    ).toEqual([])
  })
})
