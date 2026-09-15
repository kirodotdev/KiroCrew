/**
 * The English catalog, merged once, and the merge itself.
 *
 * English arrives in two files: `en.json` is REGENERATED wholesale by
 * `scripts/i18n-codemod.mjs` (so a fixed source string can't leave a stale key
 * behind), and `en.manual.json` holds hand-authored strings that have no source
 * literal to extract — like the language picker's own labels. Keeping them
 * separate is what lets the codemod overwrite its own output safely.
 *
 * `EN_TRANSLATION` lives here rather than in `./index` because BOTH catalog
 * owners need it: `./index` seeds its registry with English alone, and
 * `./catalogs` lists it alongside the other twelve. The module cache makes one
 * merge serve both, and the shared object identity is what lets
 * `registerCatalogs` recognise the English bundle it already holds instead of
 * re-merging it.
 *
 * `mergeCatalogs` is here too, with its only other caller one import away, so
 * `./index`'s graph stays at four modules. That graph is re-fetched once per test
 * file, so a module in it is not free — see `docs/testing.md` § "What a
 * `setupFiles` entry costs".
 */

import enGenerated from './locales/en.json'
import enManual from './locales/en.manual.json'

/** Deep-merge two catalogs; the later argument wins on a leaf collision. */
export function mergeCatalogs(
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

export const EN_TRANSLATION: Record<string, unknown> = mergeCatalogs(enGenerated, enManual)
