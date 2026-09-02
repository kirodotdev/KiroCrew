/**
 * Every catalog import lives here — a pure data module with no side effects and
 * no i18next dependency.
 *
 * Importing it pulls 14 modules and ~12 MB into the graph, which is why it is
 * separate from `./index`: only `./all` (the production entry) and the tests that
 * audit the full catalog set pay that cost. Under vitest that cost is charged
 * once per test FILE, and it is the module count rather than the bytes that
 * dominates. `./index`'s header carries the ownership split, the measurements,
 * and why `t()` stays synchronous regardless.
 */

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
import { EN_TRANSLATION } from './enCatalog'

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
 * A language listed in `SUPPORTED_LANGUAGES` MUST appear here; `catalogParity.test.ts`
 * asserts the two lists agree, so a language added to one and not the other fails
 * CI instead of silently rendering keys.
 *
 * Single `translation` namespace: keys are already domain-prefixed
 * (`settings.display.view`, `chat.composer.send`), which gives the same
 * grouping a namespace split would without making every call site name one.
 */
const AUTHORED_CATALOGS: Record<string, { translation: Record<string, unknown> }> = {
  en: { translation: EN_TRANSLATION },
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
 * because `t()` must remain SYNCHRONOUS — see `./index`'s header.
 */
export const CATALOGS: Record<string, { translation: Record<string, unknown> }> =
  import.meta.env.DEV
    ? { ...AUTHORED_CATALOGS, 'en-XA': { translation: enXA } }
    : AUTHORED_CATALOGS
