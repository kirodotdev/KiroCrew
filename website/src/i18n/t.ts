/**
 * Standalone translate function used by converted call sites.
 *
 * ## Why this exists instead of `useTranslation()` everywhere
 *
 * The ~250 converted components hold strings in places a React hook cannot go:
 * render callbacks (`items.map(…)`), plain helper functions outside a component,
 * and non-component modules. Calling `useTranslation()` there is a rules-of-hooks
 * violation. A plain function call is valid in all of them, which is what makes a
 * mechanical, whole-dashboard conversion possible at all.
 *
 * ## Why it is named `i18nT`
 *
 * `t` is a very common local identifier in this codebase (`.map(t => …)` over
 * tabs/turns/tasks/themes). A bare `t` import gets shadowed by those locals and
 * the call then lands on a domain object, producing `TS2349 This expression is
 * not callable` errors. `i18nT` is collision-free, and the codemod refuses to
 * convert any file that already binds the name.
 *
 * ## The trade-off, stated plainly
 *
 * A standalone `t` reads i18next's CURRENT language at call time but does not
 * subscribe to language changes — React has no idea it should re-render when the
 * language switches. Rather than paying for that per call site, it is handled
 * once centrally: `<App>` is keyed on the active language in `main.tsx`, so a
 * switch remounts the tree and every `i18nT()` re-evaluates. A language change
 * is a rare, explicit user action, so one remount is the right cost.
 *
 * `useTranslation()` remains correct and preferred for NEW code inside a
 * component body — it is finer-grained. This function is for the positions a
 * hook can't reach.
 */

import { i18next } from './index'

/**
 * Translate `key`, returning the English fallback when it is missing and the
 * key itself only if English lacks it too.
 *
 * @param key   dotted catalog path, e.g. `pages.settings.displayPanel.view`
 * @param vars  interpolation values for `{{placeholders}}` in the string
 */
export function i18nT(key: string, vars?: Record<string, unknown>): string {
  if (vars !== undefined || !i18next.isInitialized) return i18next.t(key, vars ?? {}) as string
  if (watchedStore !== i18next.store) watchStore()
  if (cachedLanguage !== i18next.language) {
    plainCache.clear()
    cachedLanguage = i18next.language
  }
  let value = plainCache.get(key)
  if (value === undefined) {
    value = i18next.t(key, {}) as string
    plainCache.set(key, value)
  }
  return value
}

/* Results of calls WITHOUT `vars`, for the active language.
 *
 * The composer and the message rows re-render on every keystroke, and each
 * i18next lookup allocates (option merging, key splitting, resource walk):
 * measured at about 0.9 MB and 5 ms per keystroke at 4x CPU throttle. A call
 * with no `vars` is a pure function of (language, key, loaded catalogs), so it
 * is cached here. A call with `vars` can depend on them (interpolation,
 * plurals via `count`, `context`) and always goes to i18next.
 *
 * The cache is dropped whenever its answer could change: the active language
 * differs from the one it was filled under (checked on every call, so no event
 * ordering matters), a language switch completes, or a catalog is added or
 * removed. The last case is the lazy loader (`./lazy`) registering a bundle
 * after first paint, which turns a fallback string into the translation. The
 * resource store only exists after `init()` and its events are not forwarded
 * to the i18next instance, so its listeners attach on the first cached call
 * and re-attach if a re-init replaces the store. */
const plainCache = new Map<string, string>()
let cachedLanguage: string | undefined
let watchedStore: typeof i18next.store | undefined
function dropPlainCache(): void {
  plainCache.clear()
}
function watchStore(): void {
  watchedStore?.off('added', dropPlainCache)
  watchedStore?.off('removed', dropPlainCache)
  watchedStore = i18next.store
  watchedStore.on('added', dropPlainCache)
  watchedStore.on('removed', dropPlainCache)
  plainCache.clear()
}
i18next.on('languageChanged', dropPlainCache)
i18next.on('initialized', dropPlainCache)
