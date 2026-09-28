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
  return i18next.t(key, vars ?? {}) as string
}
