/**
 * i18n runtime (react-i18next).
 *
 * ## Why the catalogs are imported statically
 *
 * Authored catalogs are bundled and registered up front, so `t()` is always
 * SYNCHRONOUS. That is a deliberate correctness choice, not an oversight:
 *
 *  - ~600 components call `t()` during render. With lazily-fetched catalogs
 *    every one of them becomes Suspense-sensitive, and any component rendering
 *    before its namespace resolves flashes the raw key (`settings.display.view`)
 *    or an English string that then swaps — a visible, hard-to-test artifact.
 *  - The test suite has ~4000 assertions matching visible English text. A
 *    synchronous `t()` keeps them all valid with no per-test `await`.
 *
 * The cost is bundle size: every language ships to every user (except the
 * pseudolocale, which is DEV-only — see `CATALOGS`). At 12 catalogs that is
 * ~2.0 MB gzip, ~173 KB of it for each language the user will never read, so
 * this approach does NOT scale indefinitely.
 *
 * ## Lazy-loading seam
 *
 * Korean is catalog #12 and the last one that lands in FRONT of the seam;
 * catalog #13 belongs behind it — switch to
 * `i18next-http-backend` + `Suspense`:
 * catalogs move to `public/locales/<lng>/<ns>.json` and only the active
 * language is fetched. Nothing in the call sites changes — `useTranslation()`
 * and `t()` keep the same signatures — so this is an isolated swap of THIS
 * file plus a `<Suspense>` boundary in `main.tsx`. Keeping call sites free of
 * loading concerns is exactly what makes that migration cheap later.
 */

import i18next from 'i18next'
import { initReactI18next } from 'react-i18next'

import enGenerated from './locales/en.json'
import enManual from './locales/en.manual.json'
import zhCN from './locales/zh-CN.json'
import hi from './locales/hi.json'
import es from './locales/es.json'
import fr from './locales/fr.json'
import bn from './locales/bn.json'
import pt from './locales/pt.json'
import ru from './locales/ru.json'
import de from './locales/de.json'
import ja from './locales/ja.json'
import ko from './locales/ko.json'
import it from './locales/it.json'
import enXA from './locales/en-XA.json'
import { DEFAULT_LANGUAGE, SUPPORTED_CODES } from './languages'
import { readStoredLanguage, resolveLanguage } from './detect'

/**
 * Deep-merge two catalogs (later wins on a leaf collision).
 *
 * English arrives in two files: `en.json` is REGENERATED wholesale by
 * `scripts/i18n-codemod.mjs` (so a fixed source string can't leave a stale key
 * behind), and `en.manual.json` holds hand-authored strings that have no source
 * literal to extract — like the language picker's own labels. Keeping them
 * separate is what lets the codemod overwrite its own output safely.
 */
function mergeCatalogs(
  a: Record<string, unknown>,
  b: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = { ...a }
  for (const [key, value] of Object.entries(b)) {
    const existing = out[key]
    out[key] = value !== null && typeof value === 'object' && !Array.isArray(value)
      && existing !== null && typeof existing === 'object' && !Array.isArray(existing)
      ? mergeCatalogs(existing as Record<string, unknown>, value as Record<string, unknown>)
      : value
  }
  return out
}

/**
 * Catalog registry — the SINGLE place a language's catalog is bound to its code.
 *
 * Holds the authored languages; the generated pseudolocale is appended below in
 * DEV builds only, and `CATALOGS` (not this constant) is the runtime registry.
 *
 * Exported via `CATALOGS` so tests read the same map the runtime uses instead of
 * maintaining a parallel copy. That parallel copy was a real trap: it made "add
 * a language" a 4-edit job where forgetting the 4th produced a confusing parity
 * failure pointing at the catalog rather than at the test's own map.
 *
 * A language listed in `SUPPORTED_LANGUAGES` MUST appear here; `catalogParity.test.ts` asserts the two lists agree, so a language
 * added to one and not the other fails CI instead of silently rendering keys.
 *
 * Single `translation` namespace: keys are already domain-prefixed
 * (`settings.display.view`, `chat.composer.send`), which gives the same
 * grouping a namespace split would without making every call site name one.
 */
const AUTHORED_CATALOGS: Record<string, { translation: Record<string, unknown> }> = {
  en: { translation: mergeCatalogs(enGenerated, enManual) },
  'zh-CN': { translation: zhCN },
  hi: { translation: hi },
  es: { translation: es },
  fr: { translation: fr },
  bn: { translation: bn },
  pt: { translation: pt },
  ru: { translation: ru },
  de: { translation: de },
  ja: { translation: ja },
  ko: { translation: ko },
  it: { translation: it },
}

/**
 * The pseudolocale is registered in DEV builds ONLY.
 *
 * It is unreachable in a production build by two independent guards already —
 * `devOnly` hides it from the picker and `isRestorableLanguage()` refuses it
 * from persisted state — so shipping its catalog was pure weight: the accented,
 * expansion-padded copy of every English string is the single largest catalog
 * (~88 KB gzip on first load) and no production user can select it.
 *
 * `import.meta.env.DEV` is statically replaced with `false` at build time, so
 * the ternary below is dead code in a production build and Rollup drops the
 * `en-XA.json` module with it. The import stays static (not `await import`)
 * because `t()` must remain SYNCHRONOUS — see the file header.
 */
export const CATALOGS: Record<string, { translation: Record<string, unknown> }> =
  import.meta.env.DEV
    ? { ...AUTHORED_CATALOGS, 'en-XA': { translation: enXA } }
    : AUTHORED_CATALOGS

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
    resources: CATALOGS,
    lng,
    fallbackLng: DEFAULT_LANGUAGE,
    supportedLngs: [...SUPPORTED_CODES],
    // React escapes interpolated values already; escaping here would
    // double-encode (`&amp;amp;`) any string containing & < > " '.
    interpolation: { escapeValue: false },
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
  await i18next.changeLanguage(resolveLanguage(code))
}

export { i18next }
