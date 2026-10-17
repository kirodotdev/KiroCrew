import { readFileSync } from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { transformWithOxc } from 'vite'

import { _resetSafeReloadForTests, captureSafeReload, reloadKeepingSafe } from '../lib/safeReload'

// The stale-chunk recovery in main.tsx: a lazy import 404s after a rebuild and
// the tab reloads itself. On a crash-recovery load (`?safe=1`, #12907) that
// reload can fire during boot, before the user has picked a chat, and
// `captureSafeReload` has already stripped the flag -- so a bare
// `window.location.reload()` reopens the chat that crashed the renderer.
//
// main.tsx is the composition root and cannot be imported under test, so like
// bootFailureSurface.test.ts this runs the REAL handler sliced out of the
// source (type-stripped by vite's own transform) rather than grepping it: a
// reload that is present but unreachable still fails.

const MAIN_PATH = path.resolve(__dirname, '..', 'main.tsx')

/** The `vite:preloadError` listener registration, verbatim from main.tsx. */
function handlerSource(): string {
  const src = readFileSync(MAIN_PATH, 'utf8')
  const start = src.indexOf("window.addEventListener('vite:preloadError'")
  expect(start).toBeGreaterThan(-1)
  const end = src.indexOf('\n})', start)
  expect(end).toBeGreaterThan(start)
  return src.slice(start, end + 3)
}

/** Install the real handler against a fake window; return what it did. */
async function harness(opts: { safe: boolean }) {
  const { code } = await transformWithOxc(handlerSource(), 'main-preload-handler.ts', { lang: 'ts' })
  const log: string[] = []
  const bootHref = `http://localhost:5476/?token=abc${opts.safe ? '&safe=1' : ''}`
  // The flag is captured (and stripped) at boot, before the handler can fire.
  captureSafeReload({
    location: { href: bootHref } as Location,
    history: { state: null, replaceState: () => {} } as unknown as History,
  })
  let listener: ((e: unknown) => void) | null = null
  const win = {
    addEventListener: (kind: string, fn: (e: unknown) => void) => {
      if (kind === 'vite:preloadError') listener = fn
    },
    location: {
      href: 'http://localhost:5476/?token=abc',
      reload: () => { log.push('reload') },
      replace: (url: string) => { log.push(`replace ${url}`) },
    },
  }
  const store = new Map<string, string>()
  const sessionStorage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, v) },
  }
  new Function(
    'window', 'sessionStorage', 'isCatalogChunkError', 'reloadKeepingSafe', code,
  )(win, sessionStorage, () => false, () => reloadKeepingSafe(win as unknown as Window))
  expect(listener).not.toBeNull()
  const preventDefault = vi.fn()
  listener!({ payload: new Error('Failed to fetch dynamically imported module'), preventDefault })
  return { log, preventDefault }
}

afterEach(() => _resetSafeReloadForTests())

describe('stale-chunk recovery reload (main.tsx vite:preloadError)', () => {
  it('keeps a crash-recovery load safe: reloads with safe=1 back in the address', async () => {
    const h = await harness({ safe: true })
    expect(h.preventDefault).toHaveBeenCalledTimes(1)
    expect(h.log).toEqual(['replace http://localhost:5476/?token=abc&safe=1'])
  })

  it('is a plain reload on a normal load', async () => {
    const h = await harness({ safe: false })
    expect(h.preventDefault).toHaveBeenCalledTimes(1)
    expect(h.log).toEqual(['reload'])
  })
})
