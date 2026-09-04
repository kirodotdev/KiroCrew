/**
 * i18n runtime (react-i18next).
 *
 * ## Why every catalog is registered before first render
 *
 * Catalogs are bundled and registered up front, so `t()` is always SYNCHRONOUS.
 * That is a deliberate correctness choice, not an oversight:
 *
 *  - ~600 components call `t()` during render. With lazily-fetched catalogs
 *    every one of them becomes Suspense-sensitive, and any component rendering
 *    before its namespace resolves flashes the raw key (`settings.display.view`)
 *    or an English string that then swaps — a visible, hard-to-test artifact.
 *  - The test suite has ~4000 assertions matching visible English text. A
 *    synchronous `t()` keeps them all valid with no per-test `await`.
 *
 * The cost is bundle size: every language ships to every user (except the
 * pseudolocale, which is DEV-only). At 12 catalogs that is ~2.0 MB gzip,
 * ~173 KB of it for each language the user will never read, so this approach
 * does NOT scale indefinitely.
 *
 * ## Which module owns the imports
 *
 * THIS module imports English only and seeds the registry with it. `./catalogs`
 * owns every catalog import; `./all` joins the two by calling
 * `registerCatalogs(CATALOGS)` at module scope and re-exporting this API.
 * Production boots through `./all`, so what reaches a browser is unchanged.
 *
 * The split exists for the test path: `integration/setup.ts` is a vitest
 * `setupFiles` entry, so its module graph is re-fetched once per test FILE and
 * reaching all 14 catalogs from here cost more than running the tests. The cost is
 * the per-module round trip rather than the JSON, which is why the fix is to keep
 * modules OUT of this graph and why making them cheaper to parse would not have
 * worked. Numbers, and the rules that keep the split in place, live in
 * `website/docs/testing.md` § "What a `setupFiles` entry costs" — one owner, because
 * four copies of a measurement disagree the first time anyone re-measures.
 *
 * This is ownership, not lazy loading: no load is deferred on any path, and
 * `t()` is synchronous on all of them.
 *
 * ## Lazy-loading seam
 *
 * Korean is catalog #12 and the last one that lands in FRONT of the seam;
 * catalog #13 belongs behind it — switch to
 * `i18next-http-backend` + `Suspense`:
 * catalogs move to `public/locales/<lng>/<ns>.json` and only the active
 * language is fetched. Nothing in the call sites changes — `useTranslation()`
 * and `t()` keep the same signatures — so this is an isolated swap of
 * `./catalogs` plus a `<Suspense>` boundary in `main.tsx`. `registerCatalogs` is
 * where a backend hands its fetched catalog over, so the seam needs no change to
 * this module at all.
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
 * would silently drop a REPLACEMENT catalog, which is exactly what the lazy-backend
 * seam above hands over, and ~4 s across the suite is not worth that hole.
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
      // All catalogs are preloaded, so nothing suspends. Explicit for clarity
      // and so flipping to a lazy backend is a single-line change here.
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
export async function changeLanguage(code: string): Promise<void> {
  const resolved = resolveLanguage(code)

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
        + 'English. Import from `i18n/all` rather than `i18n` — same exports, same '
        + 'synchronous `t()`, all twelve languages.',
    )
  }

  await i18next.changeLanguage(resolved)
}

export { i18next }
