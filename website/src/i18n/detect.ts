/**
 * Language resolution — pure functions, no side effects, so they are directly
 * unit-testable and safe to call before React mounts.
 *
 * Precedence (highest first):
 *   1. an explicit user choice (config `dashboard.language`, mirrored to
 *      localStorage `mc-lang` for a synchronous first paint)
 *   2. the browser's preferred languages (`navigator.languages`)
 *   3. `en`
 *
 * The explicit choice wins over the browser deliberately: a user who picks
 * English on a zh-CN machine must not be re-detected back to Chinese on the
 * next load.
 */

import {
  AUTO_LANGUAGE,
  DEFAULT_LANGUAGE,
  DETECTABLE_CODES,
  isRestorableLanguage,
} from './languages'

/** localStorage key mirroring the persisted config value (boot fast-path). */
export const LANG_STORAGE_KEY = 'mc-lang'

/**
 * Match one browser tag against the supported set, most specific first.
 *
 * `zh-CN` matches `zh-CN` exactly; a bare `zh` (or `zh-Hans`, `zh-SG`) falls
 * back to the first supported catalog sharing its primary subtag — so a
 * Chinese-preferring browser gets 简体中文 rather than English even when the
 * exact regional tag isn't one we ship.
 */
function matchTag(tag: string): string | null {
  const normalized = tag.trim()
  if (!normalized) return null

  // Exact match, case-insensitive (browsers may report `zh-cn`).
  // DETECTABLE_CODES, not SUPPORTED_CODES: a browser never sends the pseudolocale, and
  // including it would make `en` ambiguous for every real `en-*` tag.
  const exact = DETECTABLE_CODES.find(c => c.toLowerCase() === normalized.toLowerCase())
  if (exact) return exact

  // Primary-subtag match: 'zh-Hans' / 'zh' → 'zh-CN'.
  const primary = normalized.split('-')[0].toLowerCase()
  const related = DETECTABLE_CODES.find(c => c.split('-')[0].toLowerCase() === primary)
  return related ?? null
}

/**
 * A *confident* match: the tag names a supported language unambiguously.
 *
 * That covers an exact tag (`zh-CN` → `zh-CN`, case-insensitively) and a bare
 * primary subtag (`en` → `en`). It deliberately also covers a regional variant
 * of a supported code whose own primary subtag has exactly ONE supported
 * option — `en-GB`/`en-AU` → `en` — because there is no other English catalog
 * to confuse it with.
 *
 * It deliberately does NOT cover a variant that would cross into a different
 * script or region we ship separately: `zh-TW` is not a confident match for
 * `zh-CN`, because Traditional and Simplified are different scripts and
 * silently substituting one is the defect this distinction exists to prevent.
 * Those resolve only via the loose fallback in `matchTag`, and only when
 * nothing else matched.
 */
function matchConfident(tag: string): string | null {
  const normalized = tag.trim().toLowerCase()
  if (!normalized) return null

  const exact = DETECTABLE_CODES.find(c => c.toLowerCase() === normalized)
  if (exact) return exact

  // Regional variant of a supported language: confident only when the primary
  // subtag maps to a single supported code AND that code carries no region of
  // its own (so `en` matches `en-GB`, but `zh-CN` does not match `zh-TW`).
  const primary = normalized.split('-')[0]
  const candidates = DETECTABLE_CODES.filter(c => c.split('-')[0].toLowerCase() === primary)
  if (candidates.length === 1 && !candidates[0].includes('-')) return candidates[0]
  return null
}

/**
 * Best supported language for this browser, or `null` when none matches.
 *
 * Reads `navigator.languages` (ordered by user preference) and falls back to
 * the single `navigator.language`. Guarded because `navigator` is absent in
 * non-DOM test environments and `languages` is missing on older browsers.
 *
 * **Preference order dominates, but a confident match outranks a loose one.**
 * Walking the list once and taking the first *any* match let an earlier tag's
 * loose fallback outrank a later tag's exact match — `['zh-TW', 'en']` resolved
 * to `zh-CN`, serving a Traditional-Chinese reader Simplified script even though
 * they explicitly ranked English second. Preferring all exact matches globally
 * over-corrects the other way: `['en-GB', 'zh-CN']` would resolve to `zh-CN`
 * because it is exact, ignoring that the user ranked English first.
 *
 * So a tag that only matches LOOSELY (`zh-TW` → `zh-CN`, a script switch) is
 * remembered as a fallback rather than returned, letting a later CONFIDENT
 * match win. `['zh-TW', 'en']` → `en`; `['en-GB', 'zh-CN']` → `en` (en-GB is
 * confident for `en`); `['zh-TW']` alone still → `zh-CN`. See `matchConfident`
 * for where that line is drawn.
 */
export function detectBrowserLanguage(): string | null {
  if (typeof navigator === 'undefined') return null
  const tags: string[] = Array.isArray(navigator.languages) && navigator.languages.length > 0
    ? [...navigator.languages]
    : navigator.language
      ? [navigator.language]
      : []

  let looseCandidate: string | null = null
  for (const tag of tags) {
    const confident = matchConfident(tag)
    // A confident match at any position beats a loose match from an earlier
    // tag: the user named this language unambiguously, so honour it.
    if (confident) return confident
    if (looseCandidate === null) looseCandidate = matchTag(tag)
  }
  // Nothing matched exactly — fall back to the highest-ranked loose match.
  return looseCandidate
}

/**
 * Resolve the language to actually render in.
 *
 * @param stored the persisted explicit choice (config value or localStorage
 *   mirror). `''`/`undefined`/an unsupported value all mean "auto-detect" — and
 *   so does a dev-only code such as the `en-XA` pseudolocale in a production
 *   build, which is what keeps a stored pseudolocale from accenting a shipped
 *   dashboard. See `isRestorableLanguage`.
 */
export function resolveLanguage(stored?: string | null): string {
  if (stored && stored !== AUTO_LANGUAGE && isRestorableLanguage(stored)) return stored
  return detectBrowserLanguage() ?? DEFAULT_LANGUAGE
}

/**
 * The explicit choice cached in localStorage, or `''` for auto.
 *
 * Used for the synchronous first paint before the `/api/theme/boot` response
 * arrives, so a non-English user never sees an English flash on reload.
 */
export function readStoredLanguage(): string {
  try {
    const raw = localStorage.getItem(LANG_STORAGE_KEY)
    return raw && isRestorableLanguage(raw) ? raw : AUTO_LANGUAGE
  } catch {
    // Storage blocked (private mode / partitioned) — fall back to detection.
    return AUTO_LANGUAGE
  }
}
