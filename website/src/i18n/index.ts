/**
 * i18n runtime (react-i18next).
 *
 * ## Which catalogs are loaded, and when
 *
 * English is bundled with this module and is always registered: it is the
 * fallback for every missing key, so `t()` never renders a bare key.
 *
 * The browser build boots through `./lazy`, which installs a loader that
 * fetches ONE other catalog on demand as its own chunk. `main.tsx` awaits the
 * stored language's catalog before the first render, so a returning user sees
 * their language on the first paint, and `changeLanguage()` awaits the new
 * catalog before switching. `t()` itself stays SYNCHRONOUS on every path: ~600
 * components call it during render, and nothing suspends.
 *
 * `./all` registers every catalog up front instead. The tests that audit or
 * switch between catalogs use it, so their `t()` works without any `await`.
 *
 * ## Which module owns the imports
 *
 * THIS module imports English only and seeds the registry with it. `./catalogs`
 * owns every static catalog import and `./lazy` the dynamic ones.
 *
 * The split keeps the vitest setup path small: `integration/setup.ts` is a
 * `setupFiles` entry, so its module graph is re-fetched once per test FILE, and
 * reaching all 14 catalogs from here cost more than running the tests. Numbers,
 * and the rules that keep the split in place, live in `website/docs/testing.md`
 * § "What a `setupFiles` entry costs".
 */

import i18next from 'i18next'
import { initReactI18next } from 'react-i18next'

import { EN_TRANSLATION, mergeCatalogs } from './enCatalog'
import { DEFAULT_LANGUAGE, SUPPORTED_CODES } from './languages'
import { readStoredLanguage, resolveLanguage } from './detect'

/** The one namespace every catalog uses — keys carry their own domain prefix. */
const NAMESPACE = 'translation'

/**
 * The resources `initI18n()` hands to i18next, seeded English-only.
 *
 * `./catalogs` holds the full language-to-catalog map; everything beyond English
 * arrives through `registerCatalogs`.
 */
const REGISTERED_CATALOGS: Record<string, { translation: Record<string, unknown> }> = {
  en: { translation: EN_TRANSLATION },
}

/**
 * Add language catalogs to the registry.
 *
 * Works BOTH before and after `initI18n()`, and both cases are reachable:
 * production registers at module scope before init, while vitest evaluates
 * `setupFiles` — which calls `initI18n('en')` — BEFORE it imports the test file
 * that pulls `./all` in. Before init the registry is simply what
 * `init({ resources })` receives.
 *
 * After init, `addResourceBundle` is what makes the post-init case a stated
 * contract rather than a coincidence: i18next's `ResourceStore` holds the object
 * given to `init({ resources })` BY REFERENCE, so extending the registry in place
 * already reaches the live instance. Registering into the registry alone would
 * rest on that undocumented aliasing, and the day i18next copies instead, every
 * `changeLanguage` test would fall back to English rather than fail.
 *
 * That insurance is not free — `addResourceBundle` deep-copies what it is handed,
 * measured at 67-109 ms for the twelve catalogs, per file that imports `./all`. It
 * is kept unconditional anyway: skipping it for a language the store already holds
 * would silently drop a REPLACEMENT catalog, and ~4 s across the suite is not worth that hole.
 */
export function registerCatalogs(
  extra: Record<string, { translation: Record<string, unknown> }>,
): void {
  for (const [lng, bundle] of Object.entries(extra)) {
    const existing = REGISTERED_CATALOGS[lng]?.translation
    // `./all` hands back the very English bundle this module seeded, so identity
    // means there is nothing to merge -- and deep-merging ~11k leaves into
    // themselves is pure cost on a path that runs before first paint.
    if (existing === bundle.translation) continue

    const translation = existing ? mergeCatalogs(existing, bundle.translation) : bundle.translation
    REGISTERED_CATALOGS[lng] = { translation }

    if (i18next.isInitialized) {
      i18next.addResourceBundle(lng, NAMESPACE, translation, true, true)
    }
  }
}

/** Fetches one language's catalog on demand; installed by `./lazy`. */
export type CatalogLoader = (lng: string) => Promise<Record<string, unknown> | undefined>

let catalogLoader: CatalogLoader | null = null
const pendingCatalogs = new Map<string, Promise<boolean>>()
let switchSeq = 0

/** Maximum time the first paint or a language switch waits for a catalog chunk. */
export const CATALOG_LOAD_TIMEOUT_MS = 4_000

/**
 * Install the on-demand catalog loader. The production entry (`./lazy`) calls
 * this at module scope; `./all` never does, because it registers every catalog
 * up front and so has nothing left to load.
 */
export function setCatalogLoader(loader: CatalogLoader): void {
  catalogLoader = loader
}

/**
 * Make sure `lng`'s catalog is registered before anything renders in it.
 *
 * Resolves `true` at once for English, for a language already registered, and
 * when no loader is installed. Concurrent calls for one language share a single
 * fetch. Resolves `false`, never rejects, when the fetch fails or the loader
 * knows no catalog for `lng`: the caller then keeps rendering the language it
 * already has (`changeLanguage` does not switch on `false`), so `i18next.language`
 * and what is on screen stay in agreement. The pending entry is dropped either
 * way, so the next request for that language fetches again.
 */
export function ensureCatalog(lng: string): Promise<boolean> {
  if (REGISTERED_CATALOGS[lng] || !catalogLoader) return Promise.resolve(true)
  let pending = pendingCatalogs.get(lng)
  if (!pending) {
    const load = catalogLoader
    pending = new Promise<boolean>((resolve) => {
      let settled = false
      const finish = (translation?: Record<string, unknown>) => {
        if (settled) {
          // A chunk that lands after the timeout is still registered, so the
          // next request for this language is served without another fetch.
          if (translation) registerCatalogs({ [lng]: { translation } })
          return
        }
        settled = true
        clearTimeout(timer)
        if (translation) registerCatalogs({ [lng]: { translation } })
        resolve(Boolean(translation))
      }
      const timer = setTimeout(() => {
        // A chunk request can stall without rejecting. Release first paint and
        // let the provider retry rather than leaving #root empty indefinitely.
        // eslint-disable-next-line no-console -- a failed chunk fetch must be visible
        console.error(`i18n: loading the '${lng}' catalog timed out; keeping the current language`)
        finish()
      }, CATALOG_LOAD_TIMEOUT_MS)

      load(lng).then(
        finish,
        (err: unknown) => {
          // eslint-disable-next-line no-console -- a failed chunk fetch must be visible; the current language stays
          console.error(`i18n: loading the '${lng}' catalog failed; keeping the current language`, err)
          finish()
        },
      )
    }).finally(() => { pendingCatalogs.delete(lng) })
    pendingCatalogs.set(lng, pending)
  }
  return pending
}

/**
 * The product name rendered by `{{productName}}` in catalog values.
 *
 * Catalog strings never hardcode the displayed product name; they interpolate
 * this variable, supplied to i18next as `interpolation.defaultVariables`. The
 * stock build resolves it to "Kiro Crew", so rendered output is identical to a
 * hardcoded literal — the indirection exists for downstream editions.
 *
 * The `apps.<id>.manifest.*` keys are the deliberate exception: they must stay
 * byte-identical to the Python-side `app.json` prose (the manifest-sync gate),
 * so they keep the literal.
 */
const DEFAULT_PRODUCT_NAME = 'Kiro Crew'
let productName = DEFAULT_PRODUCT_NAME

/**
 * Override the product name an edition renders. Call it from the edition
 * composition root (`extensions.tsx`), which `main.tsx` imports BEFORE
 * `initI18n()` runs — after init the variable has already been handed to
 * i18next, so a late call cannot take effect and is refused loudly in dev
 * rather than half-applying.
 */
export function setProductName(name: string): void {
  if (i18next.isInitialized) {
    if (import.meta.env.DEV) {
      throw new Error('setProductName() must be called before initI18n()')
    }
    return
  }
  // Stored trimmed: accidental edge whitespace would render into every string.
  const trimmed = name.trim()
  if (trimmed) productName = trimmed
}

/**
 * Initialize i18next exactly once.
 *
 * Called from `main.tsx` before render, and from the vitest setup file so
 * every component test renders real English strings rather than bare keys.
 * Idempotent: re-invocation is a no-op, so an extra call from a test helper
 * cannot clobber a language the test just set.
 */
export function initI18n(initialLanguage?: string): typeof i18next {
  if (i18next.isInitialized) return i18next

  const lng = resolveLanguage(initialLanguage ?? readStoredLanguage())

  i18next.use(initReactI18next).init({
    resources: REGISTERED_CATALOGS,
    lng,
    fallbackLng: DEFAULT_LANGUAGE,
    supportedLngs: [...SUPPORTED_CODES],
    interpolation: {
      // React escapes interpolated values already; escaping here would
      // double-encode (`&amp;amp;`) any string containing & < > " '.
      escapeValue: false,
      // Resolves `{{productName}}` in every catalog value. A call-time
      // variable of the same name still wins, per i18next merge order.
      defaultVariables: { productName },
    },
    // A missing key renders its English fallback, never an empty string, so a
    // gap in a translation degrades to readable English instead of blank UI.
    returnEmptyString: false,
    // Keys are flat dotted paths (`settings.display.view`) resolved against a
    // NESTED catalog object, which is i18next's default `keySeparator: '.'`
    // behaviour. `nsSeparator` is disabled so a key containing ':' (e.g. a
    // label like 'Ratio: 4:3') is never mistaken for a namespace reference.
    nsSeparator: false,
    debug: false,
    react: {
      // Catalogs are registered before anything renders in their language
      // (see `ensureCatalog`), so nothing suspends.
      useSuspense: false,
    },
  })

  return i18next
}

/**
 * Switch the active language at runtime.
 *
 * Persistence is the caller's job (`useLanguage` writes config + localStorage)
 * — this only re-renders the tree. Keeping the two concerns separate lets the
 * boot path apply a server-provided language WITHOUT echoing it straight back
 * to the server.
 */
export async function changeLanguage(code: string): Promise<boolean> {
  const resolved = resolveLanguage(code)

  // Load before switching, so `languageChanged` (which drives the repaint)
  // fires only once the new catalog is in the store and no key renders raw.
  // The sequence check keeps rapid picks in click order: a slow fetch for an
  // earlier pick must not land after, and override, a later one.
  const seq = ++switchSeq
  const loaded = await ensureCatalog(resolved)
  if (seq !== switchSeq) return false

  // A catalog that did not arrive (chunk fetch failed, or the loader has none
  // for this code) leaves the language where it is. Switching anyway would make
  // `i18next.language` -- and `<html lang>`, which the provider sets from the
  // same request -- claim a language the store cannot render, so the page would
  // announce Japanese and read English. `ensureCatalog` has already reported the
  // failure and dropped its pending entry, so the next switch fetches again.
  if (!loaded) return false

  // Switching to a language nobody registered is otherwise SILENT: i18next falls
  // back to English, nothing throws, no key renders raw, and in the browser the
  // caller still sets `<html lang>` — so the page claims Japanese and reads
  // English. The only way to get here is a caller that reached the English-only
  // `./index` when it needed `./all`; `resolveLanguage` returns nothing outside
  // `SUPPORTED_CODES`, which `catalogParity.test.ts` pins against the catalog map.
  //
  // It REPORTS rather than throws, and that is deliberate. Both callers invoke
  // this as `void changeLanguage(...)`, so a rejection here would surface as an
  // unhandled rejection — which `integration/setup.ts` re-raises, killing the
  // worker on an exception attributed to no test and no file. A guard that turns a
  // wrong-language render into an unattributable shard failure costs more than it
  // explains. `isInitialized` is checked first because `hasResourceBundle` is
  // installed by `init()` and is undefined before it.
  if (import.meta.env.DEV && i18next.isInitialized
      && !i18next.hasResourceBundle(resolved, NAMESPACE)) {
    // eslint-disable-next-line no-console -- the report the block above specifies: throwing is ruled out
    console.error(
      `i18n: no catalog is registered for '${resolved}', so this switch renders `
        + 'English. Import from `i18n/lazy` (browser entries) or `i18n/all` (tests) '
        + 'rather than `i18n` — same exports, same synchronous `t()`.',
    )
  }

  await i18next.changeLanguage(resolved)
  return true
}

export { i18next }
