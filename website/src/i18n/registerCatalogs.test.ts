/**
 * `registerCatalogs` has two branches, and PRODUCTION only ever takes the first.
 *
 * `./all` calls it at module scope, before anything calls `initI18n`, so on every
 * browser boot the catalogs land in the registry that `init({ resources })` later
 * consumes. Under vitest the opposite branch runs: `integration/setup.ts` has
 * already called `initI18n('en')` by the time any test file is imported, so a test
 * that reaches for another language arrives AFTER init and is served by
 * `addResourceBundle`.
 *
 * That asymmetry is the trap: every other test in the suite exercises the post-init
 * branch, so deleting the pre-init one leaves the whole suite green while shipping a
 * dashboard that renders English to everyone. These cases pin the branch production
 * depends on, with a fresh module registry so init has not run.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

/** A catalog shaped like the real ones, small enough to assert on exactly. */
const GERMAN = { translation: { 'settings.display.view': 'Ansicht' } }

afterEach(() => {
  vi.doUnmock('i18next')
  vi.resetModules()
})

/**
 * A fresh, uninitialized i18n module graph — the state a browser boot starts from.
 *
 * `vi.resetModules()` alone is not enough: it clears the local module graph, but
 * `i18next` resolves from `node_modules`, so the same already-initialized singleton
 * comes back and the pre-init branch stays unreachable. `importActual` keeps the
 * real i18next behaviour; only the instance identity is substituted.
 *
 * The mock is registered per call with `doMock` rather than hoisted with `vi.mock`,
 * because a hoisted factory is evaluated once and its instance then carries the
 * language the previous test initialized: `initI18n` is idempotent, so the next
 * test's call returns early and silently asserts against the wrong language.
 */
async function freshI18n() {
  vi.resetModules()
  const actual = await vi.importActual<typeof import('i18next')>('i18next')
  vi.doMock('i18next', () => ({ ...actual, default: actual.default.createInstance() }))
  return import('./index')
}

describe('registerCatalogs before initI18n — the production boot path', () => {
  it('starts from an uninitialized instance, or this file tests the wrong branch', async () => {
    const { i18next } = await freshI18n()

    expect(i18next.isInitialized).toBeFalsy()
  })

  it('registers a language that initI18n then resolves', async () => {
    const { registerCatalogs, initI18n, i18next } = await freshI18n()

    registerCatalogs({ de: GERMAN })
    initI18n('de')

    expect(i18next.language).toBe('de')
    expect(i18next.t('settings.display.view')).toBe('Ansicht')
  })

  it('keeps English, so registering does not displace the seeded catalog', async () => {
    const { registerCatalogs, initI18n, i18next } = await freshI18n()

    registerCatalogs({ de: GERMAN })
    initI18n('en')

    // Asserted on a key this test did NOT supply, and on its real English value:
    // a key from GERMAN would still resolve if the seed had been replaced, and a
    // not-the-raw-key check would pass on any non-empty fallback.
    expect(i18next.t('app.changelog')).toBe('Changelog')
  })

  it('merges into a language already present instead of replacing it', async () => {
    const { registerCatalogs, initI18n, i18next } = await freshI18n()

    registerCatalogs({ de: GERMAN })
    registerCatalogs({ de: { translation: { 'chat.composer.send': 'Senden' } } })
    initI18n('de')

    expect(i18next.t('settings.display.view')).toBe('Ansicht')
    expect(i18next.t('chat.composer.send')).toBe('Senden')
  })
})

describe('registerCatalogs after initI18n — the vitest path', () => {
  it('reaches the live instance, since init has already consumed the registry', async () => {
    const { registerCatalogs, initI18n, i18next } = await freshI18n()

    initI18n('en')
    expect(i18next.isInitialized).toBe(true)

    registerCatalogs({ de: GERMAN })
    await i18next.changeLanguage('de')

    expect(i18next.t('settings.display.view')).toBe('Ansicht')
  })
})

describe('the all-languages entry registers every declared language', () => {
  /**
   * The gate in `src/test/i18nAllLanguagesEntry.test.ts` asserts static
   * REACHABILITY — that `./all` imports `./catalogs` — which is satisfied however
   * `all.ts` uses what it imports. Narrowing its one call to
   * `registerCatalogs({ en: CATALOGS.en })` therefore leaves that gate green, and
   * every other test too, while production renders English to everyone. Before the
   * split `init({ resources: CATALOGS })` made the mismatch impossible by
   * construction; this is what replaces that guarantee.
   */
  it('has a resource bundle for every code the picker offers', async () => {
    vi.resetModules()
    const { SUPPORTED_CODES } = await import('./languages')
    const { i18next, initI18n } = await import('./all')
    initI18n('en')

    const missing = SUPPORTED_CODES.filter((code) => !i18next.hasResourceBundle(code, 'translation'))

    expect(
      missing,
      'importing `i18n/all` must register every language in SUPPORTED_CODES — '
        + 'these have no catalog, so the UI renders English for them',
    ).toEqual([])
  })
})
