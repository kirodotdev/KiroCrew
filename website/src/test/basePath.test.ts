import { describe, it, expect, vi } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { resolveBuildBase, vendorImportMap, withoutServiceWorker, SW_REGISTER_CALL } from '../../scripts/lib/basePath.mjs'
import {
  BASE_PATH,
  appPathname,
  installBasePathDomShims,
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

describe('withoutServiceWorker', () => {
  it('drops the registration from the real shell', () => {
    const shell = readFileSync(path.resolve(__dirname, '../../index.html'), 'utf-8')
    expect(shell).toContain(SW_REGISTER_CALL)
    const out = withoutServiceWorker(shell)
    expect(out).not.toContain(SW_REGISTER_CALL)
    expect(out).toContain('Promise.resolve().catch')
  })

  it('refuses a shell without the call', () => {
    expect(() => withoutServiceWorker('<html></html>')).toThrow(/withoutServiceWorker/)
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

  it('reads the app route out of a page path', () => {
    expect(appPathname('/proxy/kc/embed/x', '/proxy/kc')).toBe('/embed/x')
    expect(appPathname('/proxy/kc', '/proxy/kc')).toBe('/')
    expect(appPathname('/other/embed/x', '/proxy/kc')).toBe('/other/embed/x')
    expect(appPathname('/embed/x', '')).toBe('/embed/x')
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

describe('installBasePathDomShims', () => {
  function fakeDom() {
    const calls: Array<[string, string]> = []
    class FakeElement {
      setAttribute(name: string, value: string) { calls.push([name, value]) }
    }
    class FakeImage extends FakeElement {
      _src = ''
    }
    Object.defineProperty(FakeImage.prototype, 'src', {
      configurable: true,
      get(this: FakeImage) { return this._src },
      set(this: FakeImage, v: string) { this._src = v },
    })
    class FakeAudio {
      constructor(readonly url: string) {}
    }
    const open = vi.fn()
    return {
      calls,
      open,
      FakeImage,
      win: { location: { host: 'gw.example' }, open, Element: FakeElement, HTMLImageElement: FakeImage, Audio: FakeAudio } as never,
    }
  }

  it('is a no-op in the stock build', () => {
    const { win, FakeImage } = fakeDom()
    const before = Object.getOwnPropertyDescriptor(FakeImage.prototype, 'src')
    expect(installBasePathDomShims(win)).toBe(false)
    expect(Object.getOwnPropertyDescriptor(FakeImage.prototype, 'src')).toEqual(before)
  })

  it('prefixes URL attributes, URL properties and window.open', () => {
    const { win, calls, open, FakeImage } = fakeDom()
    expect(installBasePathDomShims(win, '/proxy/kc')).toBe(true)
    const img = new FakeImage()
    img.setAttribute('src', '/api/file-raw?path=a')
    img.setAttribute('HREF', '/chat')
    img.setAttribute('class', '/not-a-url')
    img.setAttribute('src', 'https://other.example/x.png')
    expect(calls).toEqual([
      ['src', '/proxy/kc/api/file-raw?path=a'],
      ['HREF', '/proxy/kc/chat'],
      ['class', '/not-a-url'],
      ['src', 'https://other.example/x.png'],
    ])
    ;(img as unknown as { src: string }).src = '/api/artifacts/x/asset'
    expect((img as unknown as { src: string }).src).toBe('/proxy/kc/api/artifacts/x/asset')
    ;(win as unknown as { open: (u: string, t: string) => void }).open('/apps/detail/x', '_blank')
    expect(open).toHaveBeenCalledWith('/proxy/kc/apps/detail/x', '_blank')
    const audio = new (win as unknown as { Audio: new (u: string) => { url: string } }).Audio('/api/theme/t/assets/a.mp3')
    expect(audio.url).toBe('/proxy/kc/api/theme/t/assets/a.mp3')
  })
})

describe('call sites the shims cannot reach', () => {
  // A dynamic import(), a location navigation, a worklet load and a page-URL
  // route check go through neither shim, so a root-relative literal there
  // breaks a sub-path build.
  const UNREACHABLE = [
    /\bimport\(\s*(?:\/\*[^*]*\*\/\s*)?['"`]\//,
    /\blocation\.(?:assign|replace)\(\s*['"`]\//,
    /\blocation\.href\s*=\s*['"`]\//,
    /\bconst bundlePath = ['"`]\//,
    // A worklet module load, like a worker, never goes through fetch.
    /\baddModule\(\s*['"`]\//,
    // The page URL carries the base; read the app route through appPathname().
    /\bwindow\.location\.pathname\s*(?:===|!==|\.startsWith\()\s*['"`]\//,
    /\bisChatPath\(\s*window\.location\.pathname/,
  ]

  it('wrap root-relative paths in withBase', () => {
    const root = path.resolve(__dirname, '..')
    const offenders: string[] = []
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name)
        if (entry.isDirectory()) {
          if (entry.name !== 'test' && entry.name !== 'node_modules') walk(full)
        } else if (/\.tsx?$/.test(entry.name) && !/\.(test|stories)\.tsx?$/.test(entry.name)) {
          readFileSync(full, 'utf-8').split('\n').forEach((line, i) => {
            if (/^\s*(\*|\/\/)/.test(line)) return
            if (UNREACHABLE.some((re) => re.test(line))) offenders.push(`${path.relative(root, full)}:${i + 1}`)
          })
        }
      }
    }
    walk(root)
    expect(offenders).toEqual([])
  })
})
