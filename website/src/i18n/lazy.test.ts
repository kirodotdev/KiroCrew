/**
 * The browser entry (`./lazy`) registers English only and fetches every other
 * catalog on demand. These cases pin what keeps that invisible to a user: every
 * language has a loader, a switch never repaints before its catalog is in, a
 * slow fetch cannot land out of click order, and a failed fetch leaves the
 * language where it was rather than claiming one the store cannot render.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

import { CATALOGS } from './catalogs'
import ja from './locales/ja.json'

afterEach(() => {
  vi.useRealTimers()
  vi.doUnmock('i18next')
  vi.resetModules()
  vi.restoreAllMocks()
})

/**
 * A fresh i18n module graph on its own i18next instance, so this file's
 * language switches and installed loader cannot leak into the shared singleton
 * (same technique as `registerCatalogs.test.ts`).
 */
async function freshLazy() {
  vi.resetModules()
  const actual = await vi.importActual<typeof import('i18next')>('i18next')
  vi.doMock('i18next', () => ({ ...actual, default: actual.default.createInstance() }))
  return import('./lazy')
}

/** A promise plus the function that settles it, for ordering tests. */
function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

describe('lazy catalog loading', () => {
  it('has a loader for every registered language except English', async () => {
    const { CATALOG_LOADERS } = await freshLazy()

    const expected = Object.keys(CATALOGS).filter(c => c !== 'en').sort()
    expect(Object.keys(CATALOG_LOADERS).sort()).toEqual(expected)
  })

  it('registers nothing but English until a language is asked for', async () => {
    const { initI18n, i18next } = await freshLazy()
    initI18n('en')

    expect(i18next.hasResourceBundle('ja', 'translation')).toBe(false)
  })

  it('loads the catalog before switching, so the first repaint is translated', async () => {
    const { initI18n, changeLanguage, i18next } = await freshLazy()
    initI18n('en')
    const seen: string[] = []
    i18next.on('languageChanged', () => { seen.push(i18next.t('settings.secrets.title')) })

    await changeLanguage('ja')

    const expected = (ja as { settings: { secrets: { title: string } } }).settings.secrets.title
    expect(i18next.language).toBe('ja')
    expect(seen).toEqual([expected])
  })

  it('serves the stored language at boot once ensureCatalog resolves', async () => {
    const { initI18n, ensureCatalog, i18next } = await freshLazy()
    initI18n('ja')

    await ensureCatalog(i18next.language)

    const expected = (ja as { settings: { secrets: { title: string } } }).settings.secrets.title
    expect(i18next.t('settings.secrets.title')).toBe(expected)
  })

  it('shares one fetch between concurrent requests for a language', async () => {
    const { initI18n, ensureCatalog, setCatalogLoader } = await freshLazy()
    initI18n('en')
    const loader = vi.fn(async () => ({ settings: { display: { view: 'Ansicht' } } }))
    setCatalogLoader(loader)

    await Promise.all([ensureCatalog('de'), ensureCatalog('de')])
    await ensureCatalog('de')

    expect(loader).toHaveBeenCalledTimes(1)
  })

  it('keeps the later pick when an earlier pick is still loading', async () => {
    const { initI18n, changeLanguage, setCatalogLoader, i18next } = await freshLazy()
    initI18n('en')
    const slow = deferred<Record<string, unknown>>()
    setCatalogLoader(async lng => (lng === 'de' ? slow.promise : { x: lng }))

    const first = changeLanguage('de')
    await changeLanguage('fr')
    slow.resolve({ settings: { display: { view: 'Ansicht' } } })
    await first

    expect(i18next.language).toBe('fr')
  })

  it('keeps the current language and reports when a catalog fails to load', async () => {
    const { initI18n, changeLanguage, ensureCatalog, setCatalogLoader, i18next } = await freshLazy()
    initI18n('en')
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    setCatalogLoader(async () => { throw new Error('chunk 404') })

    await changeLanguage('de')

    // The store still only holds English, so the language must not claim German.
    expect(i18next.language).toBe('en')
    expect(i18next.t('settings.secrets.title')).toBe('Secrets Vault')
    expect(error).toHaveBeenCalled()
    await expect(ensureCatalog('de')).resolves.toBe(false)
  })

  it('resolves true when the catalog is present or nothing needs loading', async () => {
    const { initI18n, ensureCatalog, setCatalogLoader } = await freshLazy()
    initI18n('en')

    await expect(ensureCatalog('en')).resolves.toBe(true)
    await expect(ensureCatalog('ja')).resolves.toBe(true)
    // Already registered: no second fetch, still true.
    setCatalogLoader(async () => { throw new Error('must not be called') })
    await expect(ensureCatalog('ja')).resolves.toBe(true)
  })

  it('retries the loader on the next switch after a failure', async () => {
    const { initI18n, changeLanguage, setCatalogLoader, i18next } = await freshLazy()
    initI18n('en')
    vi.spyOn(console, 'error').mockImplementation(() => {})
    let attempts = 0
    setCatalogLoader(async () => {
      attempts += 1
      if (attempts === 1) throw new Error('chunk 404')
      return { settings: { secrets: { title: 'Geheimnis-Tresor' } } }
    })

    await changeLanguage('de')
    expect(i18next.language).toBe('en')

    await changeLanguage('de')

    expect(attempts).toBe(2)
    expect(i18next.language).toBe('de')
    expect(i18next.t('settings.secrets.title')).toBe('Geheimnis-Tresor')
  })

  it('leaves failed catalog preloads out of the stale-chunk reload path', async () => {
    const { CATALOG_LOADERS, ensureCatalog, initI18n, isCatalogLoadInFlight } = await freshLazy()
    expect(typeof isCatalogLoadInFlight).toBe('function')
    initI18n('en')
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const reloadPath = vi.fn()
    const onPreloadError = (event: Event) => {
      if (isCatalogLoadInFlight()) return
      reloadPath()
      event.preventDefault()
    }
    window.addEventListener('vite:preloadError', onPreloadError)
    const preloadError = new Event('vite:preloadError', { cancelable: true })
    CATALOG_LOADERS.de = async () => {
      await Promise.resolve()
      window.dispatchEvent(preloadError)
      throw new Error('chunk 404')
    }

    try {
      await expect(ensureCatalog('de')).resolves.toBe(false)
      expect(reloadPath).not.toHaveBeenCalled()
      expect(preloadError.defaultPrevented).toBe(false)
    } finally {
      window.removeEventListener('vite:preloadError', onPreloadError)
    }
  })
})

describe('lazy catalog failure reporting', () => {
  it('reports whether a requested switch happened', async () => {
    const { initI18n, changeLanguage, setCatalogLoader, i18next } = await freshLazy()
    initI18n('en')
    vi.spyOn(console, 'error').mockImplementation(() => {})
    setCatalogLoader(async () => { throw new Error('chunk 404') })

    await expect(changeLanguage('de')).resolves.toBe(false)
    expect(i18next.language).toBe('en')
  })

  it('bounds a stalled catalog request before first render', async () => {
    vi.useFakeTimers()
    const { CATALOG_LOAD_TIMEOUT_MS, initI18n, changeLanguage, setCatalogLoader } = await freshLazy()
    initI18n('en')
    setCatalogLoader(() => new Promise(() => {}))

    expect(CATALOG_LOAD_TIMEOUT_MS).toBe(4_000)
    const switching = changeLanguage('de')
    await vi.advanceTimersByTimeAsync(CATALOG_LOAD_TIMEOUT_MS)

    await expect(switching).resolves.toBe(false)
  })
})
