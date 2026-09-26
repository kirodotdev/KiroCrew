/**
 * The browser i18n entry: English up front, every other language on demand.
 *
 * Importing it installs a loader that fetches one catalog as its own chunk the
 * first time that language is needed, and re-exports the runtime API, so a
 * caller needs exactly one import. `main.tsx` boots through it; `./all` is the
 * eager twin the catalog-auditing tests and the crew-companion / Mochi app
 * windows use. Why a language is always registered before it renders:
 * `./index`'s header.
 */

import { setCatalogLoader } from './index'

type CatalogModule = { default: Record<string, unknown> }

/**
 * One dynamic import per authored non-English catalog, keyed by language code;
 * each becomes its own chunk. English ships with `./index`, and the
 * pseudolocale is added below in DEV builds only.
 *
 * Written out as literals rather than derived from `./catalogs` or from
 * `import.meta.glob('./locales/*.json')`: a glob has to keep `en.json`,
 * `en.manual.json` and the ~1.9 MB DEV-only `en-XA.json` out of the production
 * chunk set, which takes `!./locales/en*.json`-style negation patterns, and the
 * i18n string gate (`npm run i18n:check`) reports each of those negation
 * literals as untranslated copy. The language-to-file binding this repeats from
 * `AUTHORED_CATALOGS` is pinned by `lazy.test.ts` against `CATALOGS`, so a
 * language added there and not here fails CI.
 */
const AUTHORED_LOADERS: Record<string, () => Promise<CatalogModule>> = {
  'zh-CN': () => import('./locales/zh-CN.json'),
  hi: () => import('./locales/hi.json'),
  es: () => import('./locales/es.json'),
  fr: () => import('./locales/fr.json'),
  bn: () => import('./locales/bn.json'),
  pt: () => import('./locales/pt.json'),
  ru: () => import('./locales/ru.json'),
  de: () => import('./locales/de.json'),
  ja: () => import('./locales/ja.json'),
  ko: () => import('./locales/ko.json'),
  it: () => import('./locales/it.json'),
}

/**
 * `import.meta.env.DEV` is replaced with `false` in a production build, so the
 * pseudolocale's import is dead code there and its chunk is never emitted.
 */
export const CATALOG_LOADERS: Record<string, () => Promise<CatalogModule>> = import.meta.env.DEV
  ? { ...AUTHORED_LOADERS, 'en-XA': () => import('./locales/en-XA.json') }
  : AUTHORED_LOADERS

let catalogLoadsInFlight = 0

export function isCatalogLoadInFlight(): boolean {
  return catalogLoadsInFlight > 0
}

setCatalogLoader(async (lng) => {
  const load = CATALOG_LOADERS[lng]
  if (!load) return undefined
  catalogLoadsInFlight += 1
  try {
    return (await load()).default
  } finally {
    catalogLoadsInFlight -= 1
  }
})

export * from './index'
