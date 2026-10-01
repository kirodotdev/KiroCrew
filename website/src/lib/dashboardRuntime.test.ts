/**
 * Regression tests for the relocatable dashboard runtime (R1-A).
 *
 * test-audit authoring gate:
 *  1. Observable behavior: how every same-dashboard gateway URL family (HTTP,
 *     WebSocket, Router basename, same-dashboard navigation, and the loose
 *     transport resolver client.ts uses) resolves in each deployment mode.
 *  2. Credible regression: a change that made direct mode prefix a base, or made
 *     relay mode let a family escape the capability prefix, or made either mode
 *     rewrite an external URL, would flip these exact-string assertions.
 *  3. Existing coverage gap: there is no prior seam that owns URL relocation; the
 *     dashboard hard-coded root-relative `/api` everywhere, so nothing pinned the
 *     "direct output is byte-identical to the pre-seam URL" contract.
 *  4. Production seam: `dashboardRuntime` is required by production (main.tsx
 *     bootstraps it, client.ts and the WS owners call it). These tests use the
 *     pure resolver + injected `LocationParts`; no test-only export exists.
 */
import { describe, it, expect } from 'vitest'
import {
  resolveDashboardRuntime,
  httpPath,
  webSocketUrl,
  routerBasename,
  relocateLoose,
  type DashboardRuntime,
} from './dashboardRuntime'

const DIRECT: DashboardRuntime = { kind: 'direct', basePath: '/', routerBasename: '/' }
const RELAY = resolveDashboardRuntime({ pathname: '/instance-pane/K_cap01/chat' })

describe('resolveDashboardRuntime — boundary parsing (fail closed to direct)', () => {
  it('resolves a well-formed capability path to a relayed-pane runtime', () => {
    expect(RELAY).toEqual({
      kind: 'relayed-pane',
      basePath: '/instance-pane/K_cap01/',
      routerBasename: '/instance-pane/K_cap01',
      protocol: 1,
    })
  })

  it('treats the plain dashboard root as direct', () => {
    expect(resolveDashboardRuntime({ pathname: '/' })).toEqual(DIRECT)
    expect(resolveDashboardRuntime({ pathname: '/chat' })).toEqual(DIRECT)
    expect(resolveDashboardRuntime({ pathname: '/settings/agents' })).toEqual(DIRECT)
  })

  it('fails closed to direct on a malformed capability prefix', () => {
    // Empty capability segment, missing trailing segment, wrong prefix.
    expect(resolveDashboardRuntime({ pathname: '/instance-pane//api' }).kind).toBe('direct')
    expect(resolveDashboardRuntime({ pathname: '/instance-pane/' }).kind).toBe('direct')
    expect(resolveDashboardRuntime({ pathname: '/instance-pane' }).kind).toBe('direct')
    expect(resolveDashboardRuntime({ pathname: '/instance-panes/K_cap/x' }).kind).toBe('direct')
    expect(resolveDashboardRuntime({ pathname: '/api/instance-pane/K_cap/x' }).kind).toBe('direct')
  })

  it('rejects a capability segment carrying a slash (single segment only)', () => {
    // Only the first segment after the prefix is the capability; a nested path
    // still resolves to that one capability, never a compound one.
    const r = resolveDashboardRuntime({ pathname: '/instance-pane/K_cap01/api/ws' })
    expect(r.kind === 'relayed-pane' && r.basePath).toBe('/instance-pane/K_cap01/')
  })
})

describe('HTTP path resolution', () => {
  it('direct mode is byte-identical to the pre-seam root-relative path', () => {
    expect(httpPath(DIRECT, '/api/status')).toBe('/api/status')
    expect(httpPath(DIRECT, '/api/channels/slot/messages')).toBe('/api/channels/slot/messages')
    expect(httpPath(DIRECT, '/api/artifacts/abc?x=1#h')).toBe('/api/artifacts/abc?x=1#h')
  })

  it('relay mode places the path under the one capability prefix', () => {
    expect(httpPath(RELAY, '/api/status')).toBe('/instance-pane/K_cap01/api/status')
    expect(httpPath(RELAY, '/api/artifacts/abc?x=1#h')).toBe(
      '/instance-pane/K_cap01/api/artifacts/abc?x=1#h',
    )
  })
})

describe('WebSocket URL resolution', () => {
  const httpsLoc = { protocol: 'https:', host: 'crew.example' }
  const httpLoc = { protocol: 'http:', host: 'localhost:5476' }

  it('direct mode reproduces the exact current constructor URL', () => {
    expect(webSocketUrl(DIRECT, httpsLoc, '/api/ws?caps=slot_patch')).toBe(
      'wss://crew.example/api/ws?caps=slot_patch',
    )
    expect(webSocketUrl(DIRECT, httpLoc, '/api/ws')).toBe('ws://localhost:5476/api/ws')
    expect(webSocketUrl(DIRECT, httpsLoc, '/api/ws/stt')).toBe('wss://crew.example/api/ws/stt')
    expect(webSocketUrl(DIRECT, httpsLoc, '/api/ws/terminal/s1?cols=80')).toBe(
      'wss://crew.example/api/ws/terminal/s1?cols=80',
    )
  })

  it('relay mode keeps the socket under the capability prefix on the same origin', () => {
    expect(webSocketUrl(RELAY, httpsLoc, '/api/ws?caps=slot_patch')).toBe(
      'wss://crew.example/instance-pane/K_cap01/api/ws?caps=slot_patch',
    )
    expect(webSocketUrl(RELAY, httpsLoc, '/api/ws/terminal/s1?cols=80')).toBe(
      'wss://crew.example/instance-pane/K_cap01/api/ws/terminal/s1?cols=80',
    )
  })
})

describe('Router basename', () => {
  it('direct mode is "/" — identical to a BrowserRouter with no basename', () => {
    expect(routerBasename(DIRECT)).toBe('/')
  })
  it('relay mode is the capability prefix with no trailing slash', () => {
    expect(routerBasename(RELAY)).toBe('/instance-pane/K_cap01')
  })
})

describe('relocateLoose — the resolver client.ts helpers adopt', () => {
  it('direct mode is the identity function (byte-for-byte)', () => {
    for (const u of ['/api/x', '/api/x?y=z', 'api/rel', 'https://ext.example/y', '']) {
      expect(relocateLoose(DIRECT, u)).toBe(u)
    }
  })

  it('relay mode prefixes only root-absolute same-dashboard paths', () => {
    expect(relocateLoose(RELAY, '/api/x')).toBe('/instance-pane/K_cap01/api/x')
    expect(relocateLoose(RELAY, '/api/x?y=z')).toBe('/instance-pane/K_cap01/api/x?y=z')
  })

  it('relay mode leaves external and protocol-relative URLs untouched', () => {
    expect(relocateLoose(RELAY, 'https://fonts.googleapis.com/css2')).toBe(
      'https://fonts.googleapis.com/css2',
    )
    expect(relocateLoose(RELAY, '//cdn.example/x')).toBe('//cdn.example/x')
    expect(relocateLoose(RELAY, 'blob:https://crew.example/uuid')).toBe(
      'blob:https://crew.example/uuid',
    )
    expect(relocateLoose(RELAY, 'data:text/plain,hi')).toBe('data:text/plain,hi')
  })

  it('relay mode leaves document-relative paths untouched (already under the base)', () => {
    expect(relocateLoose(RELAY, 'api/rel')).toBe('api/rel')
    expect(relocateLoose(RELAY, './assets/x.js')).toBe('./assets/x.js')
  })

  it('relay mode never double-prefixes an already-relocated path', () => {
    expect(relocateLoose(RELAY, '/instance-pane/K_cap01/api/x')).toBe(
      '/instance-pane/K_cap01/api/x',
    )
  })
})
