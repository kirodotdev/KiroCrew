import { describe, it, expect, vi } from 'vitest'
import { resolveBuildBase, vendorImportMap } from '../../scripts/lib/basePath.mjs'
import {
  BASE_PATH,
  installBasePathShims,
  rebaseUrl,
  routerBasename,
  toBasePath,
  withBase,
} from '../lib/basePath'

describe('resolveBuildBase', () => {
  it('builds for the root when unset', () => {
    expect(resolveBuildBase(undefined)).toBe('/')
    expect(resolveBuildBase('')).toBe('/')
    expect(resolveBuildBase('  ')).toBe('/')
    expect(resolveBuildBase('/')).toBe('/')
  })

  it('normalizes a sub-path to a leading and trailing slash', () => {
    expect(resolveBuildBase('/proxy/kirocrew')).toBe('/proxy/kirocrew/')
    expect(resolveBuildBase('/proxy/kirocrew/')).toBe('/proxy/kirocrew/')
    expect(resolveBuildBase('/a//b///')).toBe('/a/b/')
  })

  it.each([
    'proxy/kirocrew',
    '//evil.example/x',
    'https://host/x',
    '/a/../b',
    '/a/./b',
    '/a?b=1',
    '/a#b',
    '/a b',
  ])('refuses %j', (raw) => {
    expect(() => resolveBuildBase(raw)).toThrow(/KIROCREW_BASE_PATH/)
  })
})

describe('vendorImportMap', () => {
  it('is unchanged for the root build', () => {
    expect(vendorImportMap('/')).toEqual({
      imports: {
        'react': '/vendor/react.mjs',
        'react-dom': '/vendor/react-dom.mjs',
        'react-dom/client': '/vendor/react-dom-client.mjs',
        'react/jsx-runtime': '/vendor/react-jsx-runtime.mjs',
        '@kirocrew/app-sdk': '/vendor/kirocrew-app-sdk.mjs',
        '@kirocrew/app-sdk/ui': '/vendor/kirocrew-ui.mjs',
        '@tanstack/react-query': '/vendor/tanstack-react-query.mjs',
        'lucide-react': '/vendor/lucide-react.mjs',
      },
    })
  })

  it('puts every stub under a sub-path base', () => {
    const { imports } = vendorImportMap('/proxy/kc/')
    for (const url of Object.values(imports)) expect(url.startsWith('/proxy/kc/vendor/')).toBe(true)
  })
})

describe('stock build', () => {
  it('has no base, so the router and transports are untouched', () => {
    expect(BASE_PATH).toBe('')
    expect(routerBasename()).toBeUndefined()
    expect(withBase('/api/x')).toBe('/api/x')
    const win = { fetch: vi.fn(), WebSocket: vi.fn(), EventSource: vi.fn(), Request, location: { host: 'h' } }
    const before = { ...win }
    expect(installBasePathShims(win as never)).toBe(false)
    expect(win).toEqual(before)
  })
})

describe('base path helpers', () => {
  it('reads Vite BASE_URL', () => {
    expect(toBasePath('/')).toBe('')
    expect(toBasePath('/proxy/kc/')).toBe('/proxy/kc')
    expect(routerBasename('/proxy/kc')).toBe('/proxy/kc')
  })

  it('prefixes root-relative paths once', () => {
    const b = '/proxy/kc'
    expect(withBase('/chat', b)).toBe('/proxy/kc/chat')
    expect(withBase('/', b)).toBe('/proxy/kc/')
    expect(withBase('/api/x?y=/z#h', b)).toBe('/proxy/kc/api/x?y=/z#h')
    expect(withBase('/proxy/kc/chat', b)).toBe('/proxy/kc/chat')
    expect(withBase('/proxy/kc', b)).toBe('/proxy/kc')
    expect(withBase('/proxy/kcx', b)).toBe('/proxy/kc/proxy/kcx')
    expect(withBase('api/x', b)).toBe('api/x')
    expect(withBase('//cdn.example/x', b)).toBe('//cdn.example/x')
  })

  it('rebases same-host absolute URLs only', () => {
    const b = '/proxy/kc'
    expect(rebaseUrl('ws://gw.example/api/ws?caps=1', 'gw.example', b)).toBe('ws://gw.example/proxy/kc/api/ws?caps=1')
    expect(rebaseUrl('https://gw.example/api/x', 'gw.example', b)).toBe('https://gw.example/proxy/kc/api/x')
    expect(rebaseUrl('https://gw.example/proxy/kc/api/x', 'gw.example', b)).toBe('https://gw.example/proxy/kc/api/x')
    expect(rebaseUrl('https://other.example/api/x', 'gw.example', b)).toBe('https://other.example/api/x')
    expect(rebaseUrl('blob:https://gw.example/abc', 'gw.example', b)).toBe('blob:https://gw.example/abc')
    expect(rebaseUrl('data:text/plain,x', 'gw.example', b)).toBe('data:text/plain,x')
  })
})

describe('installBasePathShims', () => {
  function fakeWindow() {
    const fetch = vi.fn(async () => new Response('ok'))
    class FakeSocket {
      static readonly OPEN = 1
      constructor(readonly url: string, readonly protocols?: string) {}
    }
    class FakeSource {
      constructor(readonly url: string, readonly init?: EventSourceInit) {}
    }
    return {
      fetch,
      WebSocket: FakeSocket as unknown as typeof WebSocket,
      EventSource: FakeSource as unknown as typeof EventSource,
      Request,
      location: { host: 'gw.example' },
    }
  }

  it('prefixes fetch, WebSocket and EventSource URLs', async () => {
    const win = fakeWindow()
    const nativeFetch = win.fetch
    const NativeSocket = win.WebSocket
    expect(installBasePathShims(win, '/proxy/kc')).toBe(true)

    await win.fetch('/api/health', { method: 'GET' })
    expect(nativeFetch).toHaveBeenLastCalledWith('/proxy/kc/api/health', { method: 'GET' })
    await win.fetch(new URL('https://gw.example/api/x'))
    expect(nativeFetch).toHaveBeenLastCalledWith('https://gw.example/proxy/kc/api/x', undefined)
    await win.fetch('https://other.example/api/x')
    expect(nativeFetch).toHaveBeenLastCalledWith('https://other.example/api/x', undefined)

    await win.fetch(new Request('https://gw.example/api/r', { method: 'POST', body: 'b' }))
    const sent = (nativeFetch.mock.calls.at(-1) as unknown[])[0] as Request
    expect(sent.url).toBe('https://gw.example/proxy/kc/api/r')
    expect(sent.method).toBe('POST')

    const ws = new win.WebSocket('ws://gw.example/api/ws', 'p') as unknown as { url: string; protocols: string }
    expect(ws.url).toBe('ws://gw.example/proxy/kc/api/ws')
    expect(ws.protocols).toBe('p')
    expect(ws).toBeInstanceOf(NativeSocket)
    expect(win.WebSocket.OPEN).toBe(1)

    const es = new win.EventSource('/api/file-watch?path=x') as unknown as { url: string }
    expect(es.url).toBe('/proxy/kc/api/file-watch?path=x')
  })
})
