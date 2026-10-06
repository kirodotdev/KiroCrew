/**
 * A depth guard around i18next's translate re-entry, so a cyclic resolution
 * degrades to a readable fallback instead of crashing the whole route.
 *
 * ## The failure this prevents
 *
 * i18next resolves a key by calling `Translator.translate`. Two of its paths
 * re-enter `translate` on the SAME instance:
 *
 *  - interpolation NESTING — `extendTranslation` runs `interpolator.nest`,
 *    whose callback calls `this.translate(...)` for every `$t(other.key)`
 *    reference inside a resolved value, and
 *  - the array-join path — `h &&= this.extendTranslation(h, ...)` when a value
 *    keeps resolving to an array.
 *
 * i18next's own cycle check (`lastKey?.[0] === args[0]`) only catches a value
 * that nests DIRECTLY to its own key. An INDIRECT cycle — `a` nests `b`, `b`
 * nests `a` — is not caught, so `translate` re-enters itself without bound and
 * the browser throws "too much recursion", taking down the entire React render
 * (observed on the dashboard `/chat` route). The trigger is a single catalog
 * value whose nesting forms a cycle; every other string on the route is fine.
 *
 * ## The guard
 *
 * We wrap `translator.translate` with a re-entrancy counter. JavaScript is
 * single-threaded, so one module-level counter tracks the live nesting depth
 * exactly: it rises as nested `$t(...)` references resolve and falls as each
 * returns. Legitimate nesting is shallow — a handful of levels at most — so a
 * generous ceiling never trips on real content, yet it is thousands of frames
 * below the stack limit, so a cycle is stopped long before the stack overflows.
 *
 * When the ceiling is reached we do NOT recurse further: we log once (so the
 * offending key is diagnosable in a report) and return the key itself, which is
 * exactly what i18next returns for a value it cannot resolve. The route keeps
 * rendering; only the one cyclic string shows its key instead of crashing.
 */

/**
 * The deepest legitimate `$t(...)` nesting we expect, plus wide headroom.
 *
 * Real catalogs nest a level or two (a sentence that embeds a shared noun, a
 * CTA that embeds a product term). 24 is far above anything the catalogs do and
 * far below the ~hundreds-to-thousands of frames a stack overflow needs, so it
 * separates "a deep but finite legitimate render" from "a cycle" cleanly.
 */
const MAX_TRANSLATE_DEPTH = 24

/** The i18next-instance shape we touch — just enough to wrap the translator. */
interface GuardableI18n {
  translator?: { translate: (...args: unknown[]) => unknown }
}

/**
 * Marker so a double install (e.g. a test re-init) wraps only once. A `Symbol`
 * rather than a string key: it is a private, non-colliding, non-user-facing
 * flag — never a translatable literal — and cannot clash with any real property.
 */
const guardFlag = Symbol('kiroRecursionGuard')

/**
 * Install the depth guard on an initialized i18next instance's translator.
 *
 * Idempotent: a second call on the same translator is a no-op, so re-invoking
 * it after a re-init cannot stack wrappers (each wrapper would multiply the
 * cost and shift the effective ceiling). Safe to call when the translator is
 * not yet present — it simply does nothing and the caller may try again later,
 * though in practice `initI18n` creates the translator synchronously before
 * this runs.
 *
 * Returns true when a guard is in place after the call (freshly installed or
 * already present), false when there was no translator to guard.
 */
export function installRecursionGuard(i18n: GuardableI18n): boolean {
  const translator = i18n.translator
  if (!translator) return false

  const flagged = translator as unknown as Record<symbol, unknown>
  if (flagged[guardFlag]) return true

  const original = translator.translate.bind(translator)
  let depth = 0
  // Report each distinct offending key once, so a value that renders on every
  // keystroke does not flood the console, while a second genuinely different
  // cyclic key is still surfaced.  Bounded so adversarial/dynamic keys cannot
  // grow it without limit; past the cap we stop logging new keys.
  const warnedKeys = new Set<string>()
  const WARNED_KEYS_CAP = 50

  translator.translate = function guardedTranslate(...args: unknown[]): unknown {
    if (depth >= MAX_TRANSLATE_DEPTH) {
      const keyArg = args[0]
      const keyStr = String(Array.isArray(keyArg) ? keyArg[keyArg.length - 1] : keyArg)
      // Stop the cycle before the stack does. Report once per distinct key.
      if (!warnedKeys.has(keyStr) && warnedKeys.size < WARNED_KEYS_CAP) {
        warnedKeys.add(keyStr)
        // eslint-disable-next-line no-console -- a translation cycle must be visible so it can be fixed at the source
        console.error(
          `i18n: translation nesting exceeded ${MAX_TRANSLATE_DEPTH} levels for key `
            + `'${keyStr}'; it resolves in a cycle. Rendering the key as a fallback `
            + 'instead of overflowing the stack. Fix the cyclic $t(...) reference in the catalog.',
        )
      }
      return keyStr
    }

    depth += 1
    try {
      return original(...args)
    } finally {
      depth -= 1
    }
  }

  flagged[guardFlag] = true
  return true
}
