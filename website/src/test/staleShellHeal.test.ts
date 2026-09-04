/**
 * Guards for the stale-shell self-heal: a phone's service worker can strand a
 * tab on an arbitrarily old shell (APIs live, code ancient, and iOS reloads do
 * not bypass the SW). The heal compares the RUNNING entry script against the
 * server's live index.html and breaks out exactly once per interval.
 */
import { describe, it, expect, vi } from 'vitest'

import { HEAL_PROBE_DELAY_MS, HEAL_RELOAD_MIN_INTERVAL_MS, extractEntryScript, healIfStale, installStaleShellHeal } from '../lib/staleShellHeal'

const ORIGIN = 'https://kc.example'
const html = (entry: string) =>
  `<html><head><script type="module" crossorigin src="${entry}"></script></head></html>`

function deps(overrides: Partial<Parameters<typeof healIfStale>[0]> = {}) {
  return {
    runningEntry: `${ORIGIN}/assets/index-OLD.js`,
    fetchShell: async () => ({ ok: true, text: async () => html('/assets/index-NEW.js') }),
    origin: ORIGIN,
    now: () => 1_000_000,
    readStamp: () => 0,
    writeStamp: vi.fn(),
    clearSwAndCaches: vi.fn(async () => {}),
    reload: vi.fn(),
    ...overrides,
  }
}

describe('extractEntryScript', () => {
  it('resolves the hashed module entry to an absolute URL', () => {
    expect(extractEntryScript(html('/assets/index-abc123.js'), ORIGIN))
      .toBe(`${ORIGIN}/assets/index-abc123.js`)
  })
  it('answers null for documents without a hashed entry (dev server)', () => {
    expect(extractEntryScript('<html><script src="/src/main.tsx"></script></html>', ORIGIN)).toBeNull()
  })
})

describe('healIfStale', () => {
  it('clears SW + caches and reloads when the server shell moved on', async () => {
    const d = deps()
    await expect(healIfStale(d)).resolves.toBe('healed')
    expect(d.clearSwAndCaches).toHaveBeenCalledTimes(1)
    expect(d.reload).toHaveBeenCalledTimes(1)
    expect(d.writeStamp).toHaveBeenCalledWith(1_000_000)
  })

  it('does nothing when the running entry matches the served shell', async () => {
    const d = deps({ fetchShell: async () => ({ ok: true, text: async () => html('/assets/index-OLD.js') }) })
    await expect(healIfStale(d)).resolves.toBe('fresh')
    expect(d.reload).not.toHaveBeenCalled()
  })

  it('never reloads twice within the interval (no reload loops)', async () => {
    const d = deps({ readStamp: () => 1_000_000 - HEAL_RELOAD_MIN_INTERVAL_MS + 1 })
    await expect(healIfStale(d)).resolves.toBe('skipped')
    expect(d.reload).not.toHaveBeenCalled()
  })

  it('skips quietly when offline — the fallback shell is doing its job', async () => {
    const d = deps({ fetchShell: async () => { throw new Error('offline') } })
    await expect(healIfStale(d)).resolves.toBe('skipped')
    expect(d.reload).not.toHaveBeenCalled()
  })

  it('never heals on guesswork: unrecognizable running entry or served shell', async () => {
    const noEntry = deps({ runningEntry: null })
    await expect(healIfStale(noEntry)).resolves.toBe('skipped')
    const devServed = deps({ fetchShell: async () => ({ ok: true, text: async () => '<html>login wall</html>' }) })
    await expect(healIfStale(devServed)).resolves.toBe('skipped')
    expect(devServed.reload).not.toHaveBeenCalled()
  })
})


describe('installStaleShellHeal (boot wiring)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    window.sessionStorage.clear()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    document.querySelectorAll('script[type="module"]').forEach((el) => el.remove())
  })

  function plantRunningScript(src: string) {
    const el = document.createElement('script')
    el.type = 'module'
    el.src = src
    document.head.appendChild(el)
  }

  it('heals through the real wiring: unregisters SWs, drops caches, reloads once', async () => {
    plantRunningScript('/assets/index-OLD.js')
    const unregister = vi.fn().mockResolvedValue(true)
    Object.defineProperty(window.navigator, 'serviceWorker', {
      configurable: true,
      value: { getRegistrations: vi.fn().mockResolvedValue([{ unregister }]) },
    })
    const cacheDelete = vi.fn().mockResolvedValue(true)
    Object.defineProperty(window, 'caches', {
      configurable: true,
      value: { keys: vi.fn().mockResolvedValue(['shell-v1']), delete: cacheDelete },
    })
    const reload = vi.fn()
    const origLocation = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...origLocation, origin: origLocation.origin, reload },
    })
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: () =>
        Promise.resolve('<script type="module" src="/assets/index-NEW.js"></script>'),
    })
    vi.stubGlobal('fetch', fetchMock)

    installStaleShellHeal()
    await vi.advanceTimersByTimeAsync(HEAL_PROBE_DELAY_MS + 10)

    expect(fetchMock).toHaveBeenCalledWith('/index.html', { cache: 'no-store' })
    expect(unregister).toHaveBeenCalledTimes(1)
    expect(cacheDelete).toHaveBeenCalledWith('shell-v1')
    expect(reload).toHaveBeenCalledTimes(1)
    // The stamp is written, so a second install within the interval skips.
    expect(Number(window.sessionStorage.getItem('kc-stale-shell-heal-at'))).toBeGreaterThan(0)

    Object.defineProperty(window, 'location', { configurable: true, value: origLocation })
  })

  it('still reloads when SW and CacheStorage APIs are absent (both catch arms)', async () => {
    plantRunningScript('/assets/index-OLD.js')
    Object.defineProperty(window.navigator, 'serviceWorker', {
      configurable: true,
      value: undefined,
    })
    Object.defineProperty(window, 'caches', { configurable: true, value: undefined })
    const reload = vi.fn()
    const origLocation = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...origLocation, origin: origLocation.origin, reload },
    })
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        text: () =>
          Promise.resolve('<script type="module" src="/assets/index-NEW.js"></script>'),
      }),
    )

    installStaleShellHeal()
    await vi.advanceTimersByTimeAsync(HEAL_PROBE_DELAY_MS + 10)
    expect(reload).toHaveBeenCalledTimes(1)

    Object.defineProperty(window, 'location', { configurable: true, value: origLocation })
  })

  it('does nothing when the document carries no hashed entry script', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    installStaleShellHeal()
    await vi.advanceTimersByTimeAsync(HEAL_PROBE_DELAY_MS + 10)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
