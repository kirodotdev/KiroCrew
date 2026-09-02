/**
 * Reasoning-effort vocabulary for the dashboard — mirrors the backend
 * `kiro_crew/effort.py` so the UI and server agree on levels and per-model
 * capability. Kept as a standalone module (not inside ChatInput) so it can be
 * imported without pulling in the component — and so test mocks of ChatInput
 * don't have to re-export it.
 */

import { i18nT } from '../i18n/t'

/**
 * Catalog KEY for each effort level's display label. '' = provider/model default.
 *
 * Keys, not strings: this table is evaluated at module load, so an `i18nT()` call
 * here would freeze the boot language and never re-resolve on a language switch.
 * The lookup happens in `effortLabel()`, which runs during render.
 *
 * Shaped as a flat `Record` of full literal keys, and indexed inline at the
 * `i18nT()` call, because that is the form `scripts/check-i18n-keys.mjs` can
 * resolve statically — a key it cannot resolve is a key it cannot verify exists.
 */
export const EFFORT_LABEL_KEY: Record<string, string> = {
  '': 'lib.effort.default',
  default: 'lib.effort.default',
  low: 'lib.effort.low',
  medium: 'lib.effort.medium',
  high: 'lib.effort.high',
  xhigh: 'lib.effort.xhigh',
  max: 'lib.effort.max',
}

/**
 * Localised display name for an effort level.
 *
 * A level the backend reports dynamically (via `/api/effort-levels`) that has no
 * entry above has no catalog entry either, so it is returned VERBATIM. It used to
 * be title-cased (`charAt(0).toUpperCase() + slice(1)`), which was wrong twice
 * over: it dressed a raw backend identifier up as English display copy in every
 * locale, and `toUpperCase()` is locale-insensitive, so the result was not even
 * reliably English-correct. Returning the identifier unchanged makes it legible
 * as an identifier and leaves no fabricated copy on screen.
 */
export function effortLabel(level: string): string {
  // `hasOwnProperty`, not `in`: the levels come from /api/effort-levels, so a
  // backend that reports `toString` or `constructor` would otherwise resolve to
  // an inherited Object.prototype member and hand a function to i18next.
  return Object.prototype.hasOwnProperty.call(EFFORT_LABEL_KEY, level)
    ? i18nT(EFFORT_LABEL_KEY[level])
    : level
}

/**
 * Concrete effort levels offered in the dropdown, ordered low→high, with the
 * '' default sentinel first. kiro-cli (acp) supports these on Fable/Opus/Sonnet
 * and GPT-5.x models.
 */
export const EFFORT_LEVELS = ['', 'low', 'medium', 'high', 'xhigh', 'max'] as const

/** Providers whose backend accepts a reasoning-effort level. KiroCrew is
 *  KiroACP-only, so this is just 'acp'. */
export const REASONING_EFFORT_PROVIDERS = new Set(['acp'])

/**
 * Per-model effort capability — mirrors the backend `model_supports_effort`
 * (kiro_crew/effort.py): effort is available on Fable/Opus/Sonnet and GPT-5.x
 * models; Haiku/auto/empty and the other third-party models (deepseek, minimax,
 * glm, qwen) cannot use it. Gates the dropdown so a non-capable model never
 * shows a control that would silently no-op on the backend.
 *
 * Keep this in sync with the backend allowlist — it is a conservative list of
 * known-capable families, not a "non-Claude means unsupported" denylist.
 */
export function modelSupportsEffort(model: string | undefined): boolean {
  if (!model) return false
  const m = model.toLowerCase()
  if (m === 'auto' || m.includes('haiku')) return false
  return m.includes('opus') || m.includes('sonnet') || m.includes('fable') || m.includes('gpt')
}
